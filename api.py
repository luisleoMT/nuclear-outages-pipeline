import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def _run_pipeline() -> None:
    """Run connector + data_model synchronously (called from background thread)."""
    try:
        import connector  # noqa: PLC0415
        import data_model  # noqa: PLC0415
        df = connector.run(incremental=False)
        if not df.empty:
            data_model.run()
        log.info("Background pipeline complete.")
    except Exception as exc:
        log.error("Background pipeline failed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Run extraction in background on startup if no data exists yet."""
    if not RAW_PARQUET.exists():
        log.info("No data found — running initial extraction in background...")
        asyncio.get_event_loop().run_in_executor(None, _run_pipeline)
    yield


app = FastAPI(title="Nuclear Outages API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR        = Path("data")
MODEL_DIR       = DATA_DIR / "model"
RAW_PARQUET     = MODEL_DIR / "raw_outages.parquet"
SUMMARY_PARQUET = MODEL_DIR / "daily_summary.parquet"

API_HOST = os.environ.get("API_HOST", "127.0.0.1")

APP_API_KEY    = os.environ.get("APP_API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def _is_valid_key(key: Optional[str]) -> bool:
    return not APP_API_KEY or key == APP_API_KEY

def require_auth(key: Annotated[Optional[str], Depends(api_key_header)] = None) -> None:
    if not _is_valid_key(key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )


def load_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if "period" in df.columns:
        df["period"] = pd.to_datetime(df["period"], errors="coerce")
    return df


def df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-safe list of dicts."""
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d")
    records = []
    for row in df.to_dict(orient="records"):
        records.append({k: (None if isinstance(v, float) and v != v else v)
                        for k, v in row.items()})
    return records


def _apply_date_filters(
    df: pd.DataFrame,
    start_date: Optional[date],
    end_date: Optional[date],
) -> pd.DataFrame:
    """Apply optional date range filters to a DataFrame."""
    if start_date:
        df = df[df["period"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["period"] <= pd.Timestamp(end_date)]
    return df


def _paginate_df(df: pd.DataFrame, page: int, limit: int) -> pd.DataFrame:
    offset = (page - 1) * limit
    return df.sort_values("period", ascending=False).iloc[offset: offset + limit]


# Routes

@app.get("/", tags=["Health"])
def root() -> dict:
    return {"status": "ok", "message": "Nuclear Outages API is running."}


@app.get(
    "/data",
    tags=["Data"],
    responses={
        401: {"description": "Unauthorized — invalid API key"},
        500: {"description": "Internal server error"},
    },
)
def get_data(
    _: Annotated[None, Depends(require_auth)],
    start_date: Annotated[Optional[date], Query(description="From date YYYY-MM-DD")] = None,
    end_date:   Annotated[Optional[date], Query(description="To date YYYY-MM-DD")]   = None,
    page:       Annotated[int,            Query(ge=1)]                                = 1,
    limit:      Annotated[int,            Query(ge=1, le=5000)]                       = 100,
) -> dict:
    """Return filtered daily outage records with pagination."""
    try:
        df = load_parquet(RAW_PARQUET)
        if df.empty:
            return {"total": 0, "page": page, "limit": limit, "data": []}

        df      = _apply_date_filters(df, start_date, end_date)
        total   = len(df)
        page_df = _paginate_df(df, page, limit)
        return {"total": total, "page": page, "limit": limit, "data": df_to_records(page_df)}
    except Exception as exc:
        log.error("/data error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# Track refresh status globally
_refresh_status: dict = {"running": False, "last": None, "error": None}


@app.post(
    "/refresh",
    tags=["Pipeline"],
    responses={
        403: {"description": "Forbidden — invalid EIA API key"},
        500: {"description": "Internal server error / extraction failed"},
    },
)
def refresh(
    _:    Annotated[None, Depends(require_auth)],
    full: Annotated[bool, Query(description="Force full re-extraction")] = False,
) -> dict:
    """Trigger EIA extraction in background and return immediately."""
    if _refresh_status["running"]:
        return {
            "status": "running",
            "message": "Refresh already in progress, please wait.",
        }

    # Run in background thread so the response returns immediately
    asyncio.get_event_loop().run_in_executor(None, _run_refresh, full)

    return {
        "status": "accepted",
        "message": "Refresh started in background. Call /refresh/status to check progress.",
    }


def _run_refresh(full: bool) -> None:
    """Execute connector + data_model in a background thread."""
    global _refresh_status
    _refresh_status["running"] = True
    _refresh_status["error"]   = None
    try:
        import connector   # noqa: PLC0415
        import data_model  # noqa: PLC0415
        log.info("Background refresh triggered (full=%s)", full)
        df = connector.run(incremental=not full)
        if not df.empty:
            data_model.run()
        _refresh_status["last"] = f"OK — {len(df)} rows extracted"
        log.info("Background refresh complete.")
    except Exception as exc:
        _refresh_status["error"] = str(exc)
        log.error("Background refresh failed: %s", exc, exc_info=True)
    finally:
        _refresh_status["running"] = False


@app.get("/refresh/status", tags=["Pipeline"])
def refresh_status() -> dict:
    """Check the status of the last refresh operation."""
    return {
        "running": _refresh_status["running"],
        "last_result": _refresh_status["last"],
        "error": _refresh_status["error"],
    }


@app.get(
    "/summary",
    tags=["Analytics"],
    responses={
        401: {"description": "Unauthorized — invalid API key"},
        500: {"description": "Internal server error"},
    },
)
def get_summary(
    _: Annotated[None, Depends(require_auth)],
    start_date: Annotated[Optional[date], Query()] = None,
    end_date:   Annotated[Optional[date], Query()] = None,
    page:       Annotated[int,            Query(ge=1)]         = 1,
    limit:      Annotated[int,            Query(ge=1, le=5000)] = 100,
) -> dict:
    """Return daily summary with rolling averages and day-over-day delta."""
    try:
        df = load_parquet(SUMMARY_PARQUET)
        if df.empty:
            return {"total": 0, "page": page, "limit": limit, "data": []}

        df      = _apply_date_filters(df, start_date, end_date)
        total   = len(df)
        page_df = _paginate_df(df, page, limit)
        return {"total": total, "page": page, "limit": limit, "data": df_to_records(page_df)}
    except Exception as exc:
        log.error("/summary error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api:app", host=API_HOST, port=port, reload=True)

import logging
import os
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

app = FastAPI(title="Nuclear Outages API", version="1.0.0")

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
    """Trigger EIA extraction and rebuild the data model."""
    try:
        import connector   # noqa: PLC0415
        import data_model  # noqa: PLC0415
        log.info("Refresh triggered (full=%s)", full)
        df = connector.run(incremental=not full)
        if not df.empty:
            data_model.run()
        return {
            "status": "success",
            "rows_extracted": len(df),
            "message": "Data refreshed successfully.",
        }
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Refresh failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}") from exc


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
    uvicorn.run("api:app", host=API_HOST, port=8000, reload=True)

import logging
import pandas as pd
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_DIR   = Path("data")
RAW_PARQUET = DATA_DIR / "nuclear_outages.parquet"
MODEL_DIR  = DATA_DIR / "model"


def build_model(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Construye dos tablas analíticas a partir del raw DataFrame.
    """
    df = df.copy()

    # Standardize types
    df["period"] = pd.to_datetime(df["period"], errors="coerce")
    for col in ["capacity", "outage", "percentOutage"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.sort_values("period", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Table 1: raw_outages; Clean daily logs with PK surrogate
    raw_outages = df[["period", "capacity", "outage", "percentOutage"]].copy()
    raw_outages.rename(columns={
        "outage":         "outage_mw",
        "percentOutage":  "percent_outage",
    }, inplace=True)
    raw_outages.drop_duplicates(subset=["period"], keep="last", inplace=True)
    raw_outages.insert(0, "id", range(1, len(raw_outages) + 1))
    log.info(f"raw_outages: {len(raw_outages)} rows")

    # Table 2: daily_summary; Add useful metrics: daily variation and 7-day rolling average
    summary = raw_outages[["period", "capacity", "outage_mw", "percent_outage"]].copy()
    summary["outage_mw_prev"]    = summary["outage_mw"].shift(1)
    summary["outage_mw_delta"]   = summary["outage_mw"] - summary["outage_mw_prev"]
    summary["rolling_avg_7d"]    = (
        summary["outage_mw"]
        .rolling(window=7, min_periods=1)
        .mean()
        .round(2)
    )
    summary.drop(columns=["outage_mw_prev"], inplace=True)
    log.info(f"daily_summary: {len(summary)} rows")

    return {"raw_outages": raw_outages, "daily_summary": summary}


def save_model(tables: dict[str, pd.DataFrame]) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        path = MODEL_DIR / f"{name}.parquet"
        df.to_parquet(path, index=False)
        log.info(f"Saved {name} -> {path}")


def run() -> dict[str, pd.DataFrame]:
    if not RAW_PARQUET.exists():
        raise FileNotFoundError(
            f"{RAW_PARQUET} not found. Run connector.py first."
        )

    log.info("Loading raw data...")
    raw = pd.read_parquet(RAW_PARQUET)
    log.info(f"Raw rows: {len(raw)}")

    tables = build_model(raw)
    save_model(tables)
    log.info("Data model complete.")
    return tables


if __name__ == "__main__":
    run()

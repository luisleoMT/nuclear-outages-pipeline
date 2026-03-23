import os
import time
import logging
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("connector.log"),
    ],
)
log = logging.getLogger(__name__)

EIA_BASE_URL    = "https://api.eia.gov/v2"
NUCLEAR_ROUTE   = "nuclear-outages/us-nuclear-outages/data"
PAGE_SIZE       = 5000
MAX_RETRIES     = 3
DATA_DIR        = Path("data")
PARQUET_FILE    = DATA_DIR / "nuclear_outages.parquet"
CHECKPOINT_FILE = DATA_DIR / ".last_extracted"

EIA_DATA_FIELDS = ["data[]", "data[]", "data[]"]
EIA_DATA_VALUES = ["capacity", "outage", "percentOutage"]

REQUIRED_FIELDS = ["period", "capacity", "outage"]


def get_api_key() -> str:
    """Read EIA_API_KEY from environment; raise a clear error if missing."""
    key = os.environ.get("EIA_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "EIA_API_KEY environment variable is not set.\n"
            "Get a free key at https://www.eia.gov/opendata/ then run:\n"
            "  set EIA_API_KEY=your_key_here"
        )
    return key


def build_params(api_key: str, offset: int, start_date: str | None) -> list[tuple]:
    """
    Build the query parameter list for the EIA API request.
    Uses a list of tuples to allow repeated 'data[]' keys.
    """
    params = [
        ("api_key",            api_key),
        ("frequency",          "daily"),
        ("sort[0][column]",    "period"),
        ("sort[0][direction]", "desc"),
        ("offset",             str(offset)),
        ("length",             str(PAGE_SIZE)),
    ]

    for field, value in zip(EIA_DATA_FIELDS, EIA_DATA_VALUES):
        params.append((field, value))

    if start_date:
        params.append(("start", start_date))

    return params


def fetch_page(api_key: str, offset: int, start_date: str | None = None) -> dict:
    """
    Fetch one page of outage records from the EIA API.
    Retries up to MAX_RETRIES times on network failures.
    """
    url    = f"{EIA_BASE_URL}/{NUCLEAR_ROUTE}/"
    params = build_params(api_key, offset, start_date)

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=120)

            if resp.status_code == 403:
                raise PermissionError(
                    "Invalid or missing API key. Check your EIA_API_KEY."
                )

            resp.raise_for_status()
            return resp.json()

        except PermissionError:
            raise
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = 5 * (attempt + 1)
                log.warning("Attempt %d failed: %s. Retrying in %ds…", attempt + 1, exc, wait)
                time.sleep(wait)
            else:
                log.error("Failed after %d attempts: %s", MAX_RETRIES + 1, exc)
                raise


def validate_record(record: dict) -> bool:
    """Return True only if the record contains all required non-null fields."""
    return all(record.get(f) is not None for f in REQUIRED_FIELDS)


def _paginate(api_key: str, start_date: str | None) -> list[dict]:
    """
    Internal helper: paginate through all API pages and return raw records.
    Extracted to reduce cognitive complexity of extract_all (S3776).
    """
    all_records: list[dict] = []
    offset = 0
    total: int | None = None

    while True:
        log.info("Fetching records %d – %d…", offset, offset + PAGE_SIZE - 1)
        try:
            response = fetch_page(api_key, offset, start_date)
        except requests.RequestException:
            log.error("Stopping pagination due to unrecoverable error.")
            break

        payload = response.get("response", {})

        if total is None:
            total = int(payload.get("total", 0))
            log.info("Total records available: %d", total)
            if total == 0:
                log.info("No new records found.")
                break

        records = payload.get("data", [])
        if not records:
            break

        valid   = [r for r in records if validate_record(r)]
        skipped = len(records) - len(valid)
        if skipped:
            log.warning("Skipped %d invalid records.", skipped)

        all_records.extend(valid)
        log.info("  -> %d records collected so far.", len(all_records))
        offset += PAGE_SIZE

        if offset >= total:
            break

    return all_records


def extract_all(api_key: str, incremental: bool = True) -> pd.DataFrame:
    """Paginate through the API and return a DataFrame with all records."""
    start_date = None
    if incremental and CHECKPOINT_FILE.exists():
        start_date = CHECKPOINT_FILE.read_text().strip()
        log.info("Incremental mode: fetching data since %s", start_date)
    else:
        log.info("Full extraction mode")

    all_records = _paginate(api_key, start_date)
    log.info("Extracted %d valid records total.", len(all_records))
    return pd.DataFrame(all_records)


def save_parquet(df: pd.DataFrame) -> None:
    """Merge with existing Parquet and persist, deduplicating on period."""
    DATA_DIR.mkdir(exist_ok=True)

    if PARQUET_FILE.exists():
        existing = pd.read_parquet(PARQUET_FILE)
        df = pd.concat([existing, df], ignore_index=True)
        df.drop_duplicates(subset=["period"], keep="last", inplace=True)
        log.info("Merged with existing data -> %d total rows.", len(df))

    df.to_parquet(PARQUET_FILE, index=False)
    log.info("Saved %d rows to %s.", len(df), PARQUET_FILE)


def save_checkpoint() -> None:
    """Write today's UTC date as the incremental extraction checkpoint."""
    DATA_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    CHECKPOINT_FILE.write_text(today)
    log.info("Checkpoint updated to %s.", today)


def run(incremental: bool = True) -> pd.DataFrame:
    """Main entry point: authenticate → extract → validate → save."""
    log.info("=== EIA Nuclear Outages Connector starting ===")

    api_key = get_api_key()
    df = extract_all(api_key, incremental=incremental)

    if df.empty:
        log.info("No data to save.")
        return df

    for col in ["capacity", "outage", "percentOutage"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    save_parquet(df)
    save_checkpoint()
    log.info("=== Extraction complete ===")
    return df


if __name__ == "__main__":
    import sys
    full = "--full" in sys.argv
    run(incremental=not full)

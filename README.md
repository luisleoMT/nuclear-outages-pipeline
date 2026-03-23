# ☢ Nuclear Outages Pipeline

End-to-end data pipeline that extracts U.S. nuclear outage data from the **EIA Open Data API**, stores it in Parquet files, exposes it via a REST API, and provides a web dashboard.

---

## Architecture

```
EIA API → connector.py → Parquet (raw)
                       → data_model.py → Parquet (model: plants / outages / daily_summary)
                                       → api.py (FastAPI) → frontend/index.html
```

---

## Quick Start

### 1. Clone & install dependencies

```bash
git clone <your-repo-url>
cd nuclear-outages-pipeline

pip install -r requirements.txt
```

### 2. Set your EIA API Key

Register for a free key at https://www.eia.gov/opendata/

```bash
export EIA_API_KEY="your_key_here"
```

### 3. Run the connector (extract data)

```bash
# Incremental (default — skips already-extracted dates)
python connector.py

# Full re-extraction
python connector.py --full
```

Data is saved to `data/nuclear_outages.parquet`.

### 4. Build the data model

```bash
python data_model.py
```

Creates normalized tables in `data/model/`.

### 5. Start the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Interactive docs: http://localhost:8000/docs

### 6. Open the frontend

Open `frontend/index.html` in your browser (no build step needed).  
Point it at `http://localhost:8000` (default).

---

## API Key Setup

| Variable      | Description                                      |
|---------------|--------------------------------------------------|
| `EIA_API_KEY` | Required. EIA API key for data extraction.       |
| `APP_API_KEY` | Optional. If set, all API endpoints require `X-API-Key: <value>` header. |

---

## API Endpoints

| Method | Path        | Description                                  |
|--------|-------------|----------------------------------------------|
| GET    | `/`         | Health check                                 |
| GET    | `/data`     | Query outage records (paginated + filtered)  |
| POST   | `/refresh`  | Trigger EIA extraction + model rebuild       |
| GET    | `/summary`  | Daily U.S. aggregate totals                  |

### `/data` Query Parameters

| Parameter    | Type   | Description                         |
|--------------|--------|-------------------------------------|
| `start_date` | date   | Filter from date (YYYY-MM-DD)       |
| `end_date`   | date   | Filter to date (YYYY-MM-DD)         |
| `plant_name` | string | Partial plant name (case-insensitive)|
| `plant_id`   | int    | Exact plant ID                      |
| `page`       | int    | Page number (default: 1)            |
| `limit`      | int    | Records per page (default: 100, max: 5000) |

### Example Requests

```bash
# All outages in January 2024
curl "http://localhost:8000/data?start_date=2024-01-01&end_date=2024-01-31"

# Filter by plant name
curl "http://localhost:8000/data?plant_name=Diablo+Canyon&limit=50"

# Trigger refresh
curl -X POST "http://localhost:8000/refresh"

# Full re-extraction
curl -X POST "http://localhost:8000/refresh?full=true"
```

### Example Response (`/data`)

```json
{
  "total": 1240,
  "page": 1,
  "limit": 100,
  "data": [
    {
      "period": "2024-03-15",
      "plant_id": 12,
      "outage_mw": 950.0,
      "percent_outage": 89.5,
      "plant_name": "Diablo Canyon"
    }
  ]
}
```

---

## Data Model

### Tables

**`plants`** — static plant dimension  
| Column | Type | Notes |
|--------|------|-------|
| `plant_id` | int | Primary key (surrogate) |
| `plant_name` | string | Nuclear facility name |
| `rated_capacity_mw` | float | Nameplate capacity (MW) |

**`outages`** — daily outage facts  
| Column | Type | Notes |
|--------|------|-------|
| `period` | date | Reporting date |
| `plant_id` | int | FK → plants.plant_id |
| `outage_mw` | float | MW offline |
| `percent_outage` | float | % of capacity offline |

**`daily_summary`** — pre-aggregated U.S. totals  
| Column | Type | Notes |
|--------|------|-------|
| `period` | date | Reporting date |
| `total_capacity_mw` | float | Total MW offline nationally |
| `avg_percent_outage` | float | Average % offline across plants |
| `plants_reporting` | int | Number of plants reporting |

See the ER diagram: `er_diagram.md`

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Assumptions Made

1. **EIA API v2** is used (`https://api.eia.gov/v2/nuclear-outages/us-nuclear-outages/data/`).
2. The `plantName` + `period` combination is treated as a natural unique key for deduplication.
3. `rated_capacity_mw` is taken from the latest record for each plant (it rarely changes).
4. The frontend connects to `http://localhost:8000` — change `API_BASE` in `index.html` for remote deployments.
5. Authentication is optional: the API works without `APP_API_KEY` set.

---

## Bonus Features Implemented

- ✅ **Incremental extraction** — checkpoint file tracks last run date; subsequent runs only fetch new data
- ✅ **Auth/authorization** — `APP_API_KEY` env var enables X-API-Key header validation
- ✅ **Unit + integration tests** — `tests/test_pipeline.py` covers connector, data model, and API

---

## Project Structure

```
nuclear-outages-pipeline/
├── connector.py          # Part 1 – EIA data extraction
├── data_model.py         # Part 2 – schema normalization
├── api.py                # Part 3 – FastAPI REST API
├── frontend/
│   └── index.html        # Part 4 – web dashboard
├── tests/
│   └── test_pipeline.py  # unit + integration tests
├── er_diagram.md         # ER diagram (text)
├── requirements.txt      # Python dependencies
├── data/                 # generated at runtime
│   ├── nuclear_outages.parquet
│   └── model/
│       ├── plants.parquet
│       ├── outages.parquet
│       └── daily_summary.parquet
└── README.md
```

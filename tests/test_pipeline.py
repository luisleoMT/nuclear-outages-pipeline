import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Connector tests
class TestGetApiKey:
    def test_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("EIA_API_KEY", raising=False)
        from connector import get_api_key
        with pytest.raises(EnvironmentError, match="EIA_API_KEY"):
            get_api_key()

    def test_returns_key(self, monkeypatch):
        monkeypatch.setenv("EIA_API_KEY", "test_key_123")
        from connector import get_api_key
        assert get_api_key() == "test_key_123"


class TestValidateRecord:
    def test_valid_record(self):
        from connector import validate_record
        rec = {"period": "2024-01-01", "capacity": "1000", "outage": "200"}
        assert validate_record(rec) is True

    def test_missing_field(self):
        from connector import validate_record
        rec = {"period": "2024-01-01", "capacity": "1000"}
        assert validate_record(rec) is False

    def test_none_field(self):
        from connector import validate_record
        rec = {"period": "2024-01-01", "capacity": None, "outage": "200"}
        assert validate_record(rec) is False


class TestFetchPage:
    def test_raises_on_403(self, monkeypatch):
        import requests
        from connector import fetch_page

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        monkeypatch.setattr(requests, "get", lambda *a, **kw: mock_resp)
        with pytest.raises(PermissionError, match="API key"):
            fetch_page("bad_key", offset=0)

    def test_retries_on_network_error(self, monkeypatch):
        import requests
        from connector import fetch_page

        call_count = {"n": 0}

        def mock_get(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise requests.RequestException("network error")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = lambda: None
            mock_resp.json = lambda: {"response": {"total": 0, "data": []}}
            return mock_resp

        monkeypatch.setattr(requests, "get", mock_get)
        monkeypatch.setattr("time.sleep", lambda x: None)

        result = fetch_page("key", offset=0)
        assert result["response"]["total"] == 0
        assert call_count["n"] == 3


# Data model tests

class TestBuildModel:
    @pytest.fixture
    def sample_df(self):
        """DataFrame matching the real EIA us-nuclear-outages schema (national aggregate)."""
        return pd.DataFrame([
            {"period": "2024-01-01", "capacity": "100000", "outage": "15000", "percentOutage": "15.0"},
            {"period": "2024-01-02", "capacity": "100000", "outage": "14000", "percentOutage": "14.0"},
            {"period": "2024-01-03", "capacity": "100000", "outage": "16000", "percentOutage": "16.0"},
        ])

    def test_raw_outages_table(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        assert "raw_outages" in tables
        assert len(tables["raw_outages"]) == 3
        assert "id" in tables["raw_outages"].columns
        assert "outage_mw" in tables["raw_outages"].columns

    def test_daily_summary_table(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        assert "daily_summary" in tables
        assert len(tables["daily_summary"]) == 3

    def test_daily_summary_has_analytics_columns(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        summary = tables["daily_summary"]
        assert "rolling_avg_7d" in summary.columns
        assert "outage_mw_delta" in summary.columns

    def test_deduplication_on_period(self):
        from data_model import build_model
        # Duplicate periods should be deduplicated
        df = pd.DataFrame([
            {"period": "2024-01-01", "capacity": "100000", "outage": "15000", "percentOutage": "15.0"},
            {"period": "2024-01-01", "capacity": "100000", "outage": "15500", "percentOutage": "15.5"},
        ])
        tables = build_model(df)
        assert len(tables["raw_outages"]) == 1


# API tests (FastAPI TestClient)
class TestAPI:
    @pytest.fixture(autouse=True)
    def setup_test_parquet(self, tmp_path, monkeypatch):
        """Create minimal Parquet files and patch paths so the API uses them."""
        model_dir = tmp_path / "data" / "model"
        model_dir.mkdir(parents=True)

        raw_outages = pd.DataFrame([
            {
                "id": 1,
                "period": pd.Timestamp("2024-01-01"),
                "capacity": 100000.0,
                "outage_mw": 15000.0,
                "percent_outage": 15.0,
            }
        ])
        summary = pd.DataFrame([
            {
                "period": pd.Timestamp("2024-01-01"),
                "capacity": 100000.0,
                "outage_mw": 15000.0,
                "percent_outage": 15.0,
                "outage_mw_delta": 0.0,
                "rolling_avg_7d": 15000.0,
            }
        ])

        raw_outages.to_parquet(model_dir / "raw_outages.parquet", index=False)
        summary.to_parquet(model_dir / "daily_summary.parquet", index=False)

        # Patch the Path constants inside the api module
        import api
        monkeypatch.setattr(api, "RAW_PARQUET",     model_dir / "raw_outages.parquet")
        monkeypatch.setattr(api, "SUMMARY_PARQUET", model_dir / "daily_summary.parquet")

    def test_root(self):
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        res = client.get("/")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    def test_data_endpoint_returns_records(self):
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        res = client.get("/data")
        assert res.status_code == 200
        body = res.json()
        assert "data" in body
        assert "total" in body
        assert body["total"] >= 0

    def test_data_pagination_params(self):
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        res = client.get("/data?page=1&limit=10")
        assert res.status_code == 200

    def test_summary_endpoint(self):
        from fastapi.testclient import TestClient
        import api
        client = TestClient(api.app)
        res = client.get("/summary")
        assert res.status_code == 200
        body = res.json()
        assert "data" in body

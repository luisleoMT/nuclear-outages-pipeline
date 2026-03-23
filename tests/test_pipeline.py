import json
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
        rec = {"period": "2024-01-01", "plantName": "Test Plant", "capacity": "1000", "outages": "200"}
        assert validate_record(rec) is True

    def test_missing_field(self):
        from connector import validate_record
        rec = {"period": "2024-01-01", "plantName": "Test Plant", "capacity": "1000"}
        # missing 'outages'
        assert validate_record(rec) is False

    def test_none_field(self):
        from connector import validate_record
        rec = {"period": "2024-01-01", "plantName": None, "capacity": "1000", "outages": "200"}
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
        assert call_count["n"] == 3   # 1 fail + 1 retry + 1 success


# Data model tests

class TestBuildModel:
    @pytest.fixture
    def sample_df(self):
        return pd.DataFrame([
            {"period": "2024-01-01", "plantName": "Plant A", "capacity": "1000", "outages": "200", "percentOutage": "20"},
            {"period": "2024-01-01", "plantName": "Plant B", "capacity": "800",  "outages": "400", "percentOutage": "50"},
            {"period": "2024-01-02", "plantName": "Plant A", "capacity": "1000", "outages": "100", "percentOutage": "10"},
        ])

    def test_plants_table(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        assert "plants" in tables
        assert len(tables["plants"]) == 2
        assert "plant_id" in tables["plants"].columns

    def test_outages_table_deduplication(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        outages = tables["outages"]
        # 2 plants × 2 periods - 1 missing = 3 rows
        assert len(outages) == 3

    def test_daily_summary(self, sample_df):
        from data_model import build_model
        tables = build_model(sample_df)
        summary = tables["daily_summary"]
        assert "total_capacity_mw" in summary.columns
        assert "avg_percent_outage" in summary.columns
        assert len(summary) == 2   # 2 distinct periods


# API tests (FastAPI TestClient)

class TestAPI:
    @pytest.fixture(autouse=True)
    def setup_test_parquet(self, tmp_path, monkeypatch):
        """Create minimal parquet files so the API doesn't fail."""
        model_dir = tmp_path / "data" / "model"
        model_dir.mkdir(parents=True)

        plants = pd.DataFrame([
            {"plant_id": 1, "plant_name": "Plant A", "rated_capacity_mw": 1000.0},
        ])
        outages = pd.DataFrame([
            {"period": pd.Timestamp("2024-01-01"), "plant_id": 1, "outage_mw": 200.0, "percent_outage": 20.0},
        ])
        summary = pd.DataFrame([
            {"period": pd.Timestamp("2024-01-01"), "total_capacity_mw": 200.0, "avg_percent_outage": 20.0, "plants_reporting": 1},
        ])

        plants.to_parquet(model_dir / "plants.parquet", index=False)
        outages.to_parquet(model_dir / "outages.parquet", index=False)
        summary.to_parquet(model_dir / "daily_summary.parquet", index=False)

        # Patch paths in api module
        monkeypatch.chdir(tmp_path)

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

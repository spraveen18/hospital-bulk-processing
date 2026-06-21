# tests/test_bulk.py

import pytest
import httpx
import respx
from fastapi.testclient import TestClient
from httpx import AsyncClient, Response

from app.main import app
from app.batch_processor import parse_csv, job_store, JobStatus


# ─────────────────────────────────────────
# UNIT TESTS: parse_csv
# ─────────────────────────────────────────

class TestParseCSV:

    def test_valid_csv_all_fields(self):
        csv = b"name,address,phone\nGeneral Hospital,123 Main St,555-0001"
        rows = parse_csv(csv)
        assert len(rows) == 1
        assert rows[0].name == "General Hospital"
        assert rows[0].address == "123 Main St"
        assert rows[0].phone == "555-0001"
        assert rows[0].row == 1

    def test_valid_csv_optional_phone(self):
        csv = b"name,address,phone\nGeneral Hospital,123 Main St,"
        rows = parse_csv(csv)
        assert rows[0].phone is None

    def test_valid_csv_no_phone_column(self):
        csv = b"name,address\nGeneral Hospital,123 Main St"
        rows = parse_csv(csv)
        assert rows[0].phone is None

    def test_missing_name_column(self):
        csv = b"address,phone\n123 Main St,555-0001"
        with pytest.raises(ValueError, match="'name' and 'address' columns"):
            parse_csv(csv)

    def test_missing_address_column(self):
        csv = b"name,phone\nGeneral Hospital,555-0001"
        with pytest.raises(ValueError, match="'name' and 'address' columns"):
            parse_csv(csv)

    def test_empty_file(self):
        with pytest.raises(ValueError, match="empty"):
            parse_csv(b"")

    def test_empty_name_row(self):
        csv = b"name,address\n,123 Main St"
        with pytest.raises(ValueError, match="Row 1"):
            parse_csv(csv)

    def test_empty_address_row(self):
        csv = b"name,address\nGeneral Hospital,"
        with pytest.raises(ValueError, match="Row 1"):
            parse_csv(csv)

    def test_exceeds_max_hospitals(self):
        # Build CSV with 21 rows
        lines = ["name,address"]
        for i in range(21):
            lines.append(f"Hospital {i},Address {i}")
        csv = "\n".join(lines).encode()
        with pytest.raises(ValueError, match="exceeds maximum"):
            parse_csv(csv)

    def test_exactly_max_hospitals(self):
        # 20 rows should pass
        lines = ["name,address"]
        for i in range(20):
            lines.append(f"Hospital {i},Address {i}")
        csv = "\n".join(lines).encode()
        rows = parse_csv(csv)
        assert len(rows) == 20

    def test_multiple_row_errors_reported_together(self):
        csv = b"name,address\n,123 Main St\nHospital B,\n,\n"
        with pytest.raises(ValueError) as exc:
            parse_csv(csv)
        error = str(exc.value)
        assert "Row 1" in error
        assert "Row 2" in error
        assert "Row 3" in error

    def test_row_numbers_are_correct(self):
        csv = b"name,address\nHospital A,Addr A\nHospital B,Addr B"
        rows = parse_csv(csv)
        assert rows[0].row == 1
        assert rows[1].row == 2


# ─────────────────────────────────────────
# INTEGRATION TESTS: API endpoints
# ─────────────────────────────────────────

VALID_CSV = b"name,address,phone\nGeneral Hospital,123 Main St,555-0001\nCity Clinic,456 Oak Ave,"
INVALID_CSV = b"name,address\n,123 Main St\nHospital B,"


@pytest.fixture
def client():
    return TestClient(app)


class TestValidateCSV:

    def test_valid_csv_returns_valid_true(self, client):
        response = client.post(
            "/hospitals/validate-csv",
            files={"file": ("hospitals.csv", VALID_CSV, "text/csv")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["total_rows"] == 2
        assert data["errors"] == []

    def test_invalid_csv_returns_errors(self, client):
        response = client.post(
            "/hospitals/validate-csv",
            files={"file": ("hospitals.csv", INVALID_CSV, "text/csv")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert len(data["errors"]) == 2

    def test_wrong_file_type_rejected(self, client):
        response = client.post(
            "/hospitals/validate-csv",
            files={"file": ("hospitals.txt", VALID_CSV, "text/plain")},
        )
        assert response.status_code == 400

    def test_empty_file_rejected(self, client):
        response = client.post(
            "/hospitals/validate-csv",
            files={"file": ("hospitals.csv", b"", "text/csv")},
        )
        assert response.status_code == 400


class TestBulkCreate:

    def test_bulk_returns_202_with_job_id(self, client):
        response = client.post(
            "/hospitals/bulk",
            files={"file": ("hospitals.csv", VALID_CSV, "text/csv")},
        )
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "accepted"
        assert "/hospitals/bulk/" in data["poll_url"]

    def test_wrong_file_type_rejected(self, client):
        response = client.post(
            "/hospitals/bulk",
            files={"file": ("hospitals.txt", VALID_CSV, "text/plain")},
        )
        assert response.status_code == 400

    def test_empty_file_rejected(self, client):
        response = client.post(
            "/hospitals/bulk",
            files={"file": ("hospitals.csv", b"", "text/csv")},
        )
        assert response.status_code == 400

    def test_invalid_csv_returns_422(self, client):
        response = client.post(
            "/hospitals/bulk",
            files={"file": ("hospitals.csv", INVALID_CSV, "text/csv")},
        )
        # parse_csv raises ValueError → 422
        assert response.status_code == 422


class TestJobStatus:

    def test_unknown_job_returns_404(self, client):
        response = client.get("/hospitals/bulk/non-existent-job-id")
        assert response.status_code == 404

    def test_known_job_returns_status(self, client):
        # Submit a job first
        post_response = client.post(
            "/hospitals/bulk",
            files={"file": ("hospitals.csv", VALID_CSV, "text/csv")},
        )
        job_id = post_response.json()["job_id"]

        # Poll it
        get_response = client.get(f"/hospitals/bulk/{job_id}")
        assert get_response.status_code == 200
        data = get_response.json()
        assert data["job_id"] == job_id
        assert data["status"] in ["pending", "processing", "done", "failed"]


class TestResume:

    def test_resume_nonexistent_job_returns_404(self, client):
        response = client.post("/hospitals/bulk/fake-job-id/resume")
        assert response.status_code == 404

    def test_resume_completed_job_returns_400(self, client):
        # Manually inject a completed job into the store
        job_id = "test-completed-job"
        job_store.create(job_id)

        from app.models import BulkCreateResponse
        fake_result = BulkCreateResponse(
            batch_id="some-batch",
            total_hospitals=1,
            processed_hospitals=1,
            failed_hospitals=0,
            processing_time_seconds=1.0,
            batch_activated=True,
            hospitals=[],
        )
        job_store.mark_done(job_id, fake_result)

        response = client.post(f"/hospitals/bulk/{job_id}/resume")
        assert response.status_code == 400
        assert "already completed" in response.json()["detail"]


class TestHealth:

    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ─────────────────────────────────────────
# ERROR SCENARIO TESTS: external API failure
# ─────────────────────────────────────────

class TestExternalAPIFailure:

    @pytest.mark.asyncio
    @respx.mock
    async def test_hospital_creation_timeout_captured(self):
        """
        If external API times out, error is captured per row, not raised globally.
        """
        from app.hospital_client import create_hospital
        import httpx

        respx.post("https://hospital-directory.onrender.com/hospitals/").mock(
            side_effect=httpx.TimeoutException("timed out")
        )

        from app.models import HospitalRow
        from app.batch_processor import _create_one, job_store
        import asyncio

        job_id = "timeout-test-job"
        job_store.create(job_id)

        row = HospitalRow(row=1, name="Test", address="Addr")
        semaphore = asyncio.Semaphore(1)
        counter = [0]

        result = await _create_one(row, "batch-123", semaphore, job_id, 1, counter)

        assert result.hospital_id is None
        assert result.status.value == "failed"
        assert "Timeout" in result.error

    @pytest.mark.asyncio
    @respx.mock
    async def test_hospital_creation_http_error_captured(self):
        """
        If external API returns 500, error message includes status code.
        """
        respx.post("https://hospital-directory.onrender.com/hospitals/").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        from app.models import HospitalRow
        from app.batch_processor import _create_one, job_store
        import asyncio

        job_id = "http-error-test-job"
        job_store.create(job_id)

        row = HospitalRow(row=1, name="Test", address="Addr")
        semaphore = asyncio.Semaphore(1)
        counter = [0]

        result = await _create_one(row, "batch-123", semaphore, job_id, 1, counter)

        assert result.hospital_id is None
        assert "500" in result.error
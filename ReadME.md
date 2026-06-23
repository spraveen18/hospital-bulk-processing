# Hospital Bulk Processing System

A production-grade bulk CSV processing API built with FastAPI.
Integrates with the Hospital Directory API to create and activate hospitals in batches.

---

## Design Decisions

### Why FastAPI over Flask?
- Native async/await — HTTP calls to external API run concurrently, not sequentially
- BackgroundTasks — bulk job starts immediately, client gets job_id without waiting
- Pydantic — request/response validation with zero boilerplate

### Concurrency Model
Each bulk upload fires all hospital creation calls concurrently using asyncio.gather
controlled by a semaphore (default: 5 concurrent calls). This keeps the external API
from being overwhelmed while still being significantly faster than sequential processing.

    Sequential (20 hospitals @ 500ms each) → ~10 seconds
    Concurrent (20 hospitals, semaphore=5) → ~2 seconds

### Async Job Pattern
POST /hospitals/bulk returns a job_id immediately (202 Accepted).
Processing happens in the background. Client polls GET /hospitals/bulk/{job_id}
until status is done or failed.

### Resume Capability
If a batch partially fails, the same job_id and batch_id are preserved.
POST /hospitals/bulk/{job_id}/resume retries only the failed rows —
succeeded rows are never re-created.

### Validate Before Upload
POST /hospitals/validate-csv checks your CSV format without making any
external API calls. Use this before /bulk to catch errors early.

---

## Project Structure

    hospital-bulk-processing/
    ├── app/
    │   ├── main.py              # FastAPI app + lifespan
    │   ├── config.py            # Env-based configuration
    │   ├── models.py            # Pydantic request/response models
    │   ├── hospital_client.py   # HTTP client for external API
    │   ├── batch_processor.py   # Core bulk processing logic + job state
    │   └── routers/
    │       └── hospitals.py     # Route definitions
    ├── tests/
    │   └── test_bulk.py         # 27 tests: unit + integration + error scenarios
    ├── conftest.py
    ├── pytest.ini
    ├── requirements.txt
    ├── Dockerfile
    └── docker-compose.yml

---

## API Endpoints

| Method | Endpoint                          | Description                            |
|--------|-----------------------------------|----------------------------------------|
| POST   | /hospitals/bulk                   | Upload CSV, start bulk job (202)       |
| GET    | /hospitals/bulk/{job_id}          | Poll job progress and result           |
| POST   | /hospitals/bulk/{job_id}/resume   | Resume a failed or partial job         |
| POST   | /hospitals/validate-csv           | Validate CSV without creating hospitals|
| GET    | /health                           | Health check                           |

---

## CSV Format

    name,address,phone
    General Hospital,123 Main St,555-0001
    City Clinic,456 Oak Ave,
    Downtown Medical,789 Pine Rd,555-0003

- name — required
- address — required
- phone — optional
- Maximum 20 hospitals per CSV

---

## Setup (Local)

### Prerequisites
- Python 3.9+
- pip

### Install and run

    git clone <your-repo-url>
    cd hospital-bulk-processing

    python3 -m venv venv
    source venv/bin/activate

    pip install -r requirements.txt

    uvicorn app.main:app --reload --port 8000

API available at:  http://localhost:8000
Swagger docs at:   http://localhost:8000/docs

---

## Setup (Docker)

    docker-compose up

Or manually:

    docker build -t hospital-bulk-processing .
    docker run -p 8000:8000 hospital-bulk-processing

### Environment Variables

| Variable           | Default | Description                              |
|--------------------|---------|------------------------------------------|
| CONCURRENCY_LIMIT  | 5       | Max concurrent hospital creation calls   |
| MAX_HOSPITALS      | 20      | Max hospitals per CSV upload             |

---

## Usage Examples

### 1. Validate CSV first

    curl -X POST http://localhost:8000/hospitals/validate-csv \
      -F "file=@hospitals.csv"

    Response:
    {
      "valid": true,
      "total_rows": 4,
      "errors": []
    }

### 2. Submit bulk job

    curl -X POST http://localhost:8000/hospitals/bulk \
      -F "file=@hospitals.csv"

    Response:
    {
      "job_id": "b3cf2387-3fed-43d9-b1f6-2ab5f3464e09",
      "status": "accepted",
      "poll_url": "/hospitals/bulk/b3cf2387-3fed-43d9-b1f6-2ab5f3464e09"
    }

### 3. Poll for progress

    curl http://localhost:8000/hospitals/bulk/b3cf2387-3fed-43d9-b1f6-2ab5f3464e09

    Response:
    {
      "job_id": "b3cf2387-3fed-43d9-b1f6-2ab5f3464e09",
      "status": "done",
      "total": 4,
      "completed": 4,
      "failed_count": 0,
      "result": {
        "batch_id": "1cb561be-a862-40f4-a2b5f3464e09",
        "total_hospitals": 4,
        "processed_hospitals": 4,
        "failed_hospitals": 0,
        "processing_time_seconds": 5.703,
        "batch_activated": true,
        "hospitals": [
          {
            "row": 1,
            "hospital_id": 6,
            "name": "General Hospital",
            "status": "created_and_activated",
            "error": null
          }
        ]
      }
    }

### 4. Resume a failed job

    curl -X POST http://localhost:8000/hospitals/bulk/b3cf2387-3fed-43d9-b1f6-2ab5f3464e09/resume

    Response:
    {
      "job_id": "b3cf2387-3fed-43d9-b1f6-2ab5f3464e09",
      "status": "resuming",
      "poll_url": "/hospitals/bulk/b3cf2387-3fed-43d9-b1f6-2ab5f3464e09"
    }

---

## Running Tests

    pip install pytest pytest-asyncio respx
    pytest tests/ -v

Expected output: 27 passed

---

## Live Demo

Public URL: https://hospital-bulk-processing-7lkn.onrender.com

---

## Tech Stack

- FastAPI — async web framework
- httpx — async HTTP client with connection pooling
- Pydantic v2 — data validation
- uvicorn — ASGI server
- pytest + respx — testing with mocked external HTTP calls
- Docker — containerized deployment

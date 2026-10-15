# Employee Attendance & Analytics API

FastAPI + MongoDB backend implementation for employee attendance tracking and analytics, adhering to OpenAPI 3.0.3 contract and business rules R1–R10.

## Tech Stack & Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0 / 8.3)
- FastAPI, Uvicorn, PyMongo, Pydantic v2

## Environment Setup & Configuration

1. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   # On Linux/macOS:
   source .venv/bin/activate
   # On Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables**:
   Copy the example file and edit as needed:
   ```bash
   cp .env.example .env
   # Then open .env and adjust MONGO_URI / MONGO_DB if required.
   ```
   The defaults (`mongodb://localhost:27017` / `attendance_db`) work for a local MongoDB instance.
   Real environment variables always take precedence over `.env` values.

4. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

## Running the Application

Start the API server from the repository root:
```bash
uvicorn app.main:app --port 8000
```
- The readiness probe is available at `GET /health` (returns 200 when MongoDB is connected).
- Database indexes are created idempotently during application startup.

## API Documentation
Once the server is running, interactive documentation is available at:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- OpenAPI Specification: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

## Running Automated Tests

Run the full test suite covering business rules R1–R10, concurrency races, edge cases, and analytics.
Requires a running MongoDB instance (configured via `MONGO_URI` / `MONGO_DB`):
```bash
pytest tests/test_api.py -v
```
Expected: **12 passed**.

## MongoDB Explain Endpoint

Inspect query plans and execution statistics with `executionStats` verbosity:
- `GET /admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31`
- `GET /admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07`
- `GET /admin/explain/department_summary?month=2026-07`
- `GET /admin/explain/late_leaderboard?month=2026-07`
- `GET /admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10`

All 5 operations utilize index scans (`IXSCAN`) and execute without collection scans (`COLLSCAN`) even across 100,000+ attendance records.

## Known Limitations
- The API expects single-region UTC/IST deployments with MongoDB 6.0+ supporting window functions (`$setWindowFields`) and gap-filling (`$densify`).
- Authentication and authorization are out of scope for this assignment as specified in the problem statement.

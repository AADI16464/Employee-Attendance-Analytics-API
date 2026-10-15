"""Employee Attendance & Analytics API - Production Implementation.

Contract: openapi.yaml
Data Model: DATA_MODEL.md
Business Rules: R1 - R10
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import os
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("attendance_api")

# Timezones
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Environment & MongoDB Client
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Indexes & Lifespan Management
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # Unique business key for employees
        db.employees.create_index([("emp_code", 1)], unique=True)
        # Supporting index for department filtering and join date checks
        db.employees.create_index([("department", 1), ("joined_on", 1)])

        # Unique compound key per employee and attendance date
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        # Punch-out lookup of most recent punch-in
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        # List attendance deterministic sort and date range filtering
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        # Date and late minutes index for leaderboard aggregation
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
        # Date and status index
        db.attendance_logs.create_index([("date", 1), ("status", 1)])
        logger.info("Successfully created/verified all MongoDB indexes.")
    except Exception as e:
        logger.error("Failed to ensure indexes: %s", e)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes idempotently
    ensure_indexes()
    yield
    # Shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions: Rounding, Timestamps, Business Rules R1-R5
# --------------------------------------------------------------------------- #
def truncate_ms_to_sec(ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def now_utc_epoch_ms() -> int:
    """Returns current instant in epoch milliseconds truncated to whole seconds."""
    now_sec = int(datetime.now(UTC).timestamp())
    return now_sec * 1000


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """Converts epoch ms to whole-second truncated UTC datetime."""
    ms = truncate_ms_to_sec(ms)
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts UTC datetime (naive or aware) to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def round_half_up_2(val: Optional[float]) -> Optional[float]:
    """R8: 2 decimal places half-up rounding."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    """R8: 4 decimal places half-up rounding."""
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: Overnight shift if shift_end <= shift_start."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """R1: Computes the IST attendance date for a punch-in.

    For overnight shifts, a punch-in whose IST time of day is strictly earlier
    than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ms = truncate_ms_to_sec(punch_in_ms)
    punch_utc = datetime.fromtimestamp(punch_in_ms / 1000, tz=UTC)
    punch_ist = punch_utc.astimezone(IST)

    if is_overnight_shift(shift_start, shift_end):
        punch_time_str = punch_ist.strftime("%H:%M")
        if punch_time_str < shift_end:
            return (punch_ist.date() - timedelta(days=1)).isoformat()

    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R2: Grace period is 10 minutes.

    Late only if strictly more than 10:00 (600s) after shift_start.
    When late, whole minutes (floor) elapsed since shift_start; otherwise 0.
    """
    punch_in_ms = truncate_ms_to_sec(punch_in_ms)
    punch_utc = datetime.fromtimestamp(punch_in_ms / 1000, tz=UTC)
    punch_ist = punch_utc.astimezone(IST)

    sh, sm = map(int, shift_start.split(":"))
    att_date = dt_date.fromisoformat(attendance_date)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)

    elapsed_sec = int((punch_ist - shift_start_dt).total_seconds())
    if elapsed_sec > 600:
        return elapsed_sec // 60
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    punch_in_ms = truncate_ms_to_sec(punch_in_ms)
    punch_out_ms = truncate_ms_to_sec(punch_out_ms)
    duration_sec = (punch_out_ms - punch_in_ms) // 1000
    hours = Decimal(duration_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_ms: int, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3: Whole minutes (floor) between shift_end and punch_out, only when >= 30; otherwise 0."""
    punch_out_ms = truncate_ms_to_sec(punch_out_ms)
    punch_utc = datetime.fromtimestamp(punch_out_ms / 1000, tz=UTC)
    punch_ist = punch_utc.astimezone(IST)

    eh, em = map(int, shift_end.split(":"))
    att_date = dt_date.fromisoformat(attendance_date)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    elapsed_sec = int((punch_ist - shift_end_dt).total_seconds())
    if elapsed_sec >= 1800:
        return elapsed_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict) -> dict:
    at_val = entry.get("at")
    at_ms = utc_dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
    changes_out = {}
    for k, v in entry.get("changes", {}).items():
        if isinstance(v, dict):
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_v = utc_dt_to_epoch_ms(from_v) if isinstance(from_v, datetime) else from_v
                to_v = utc_dt_to_epoch_ms(to_v) if isinstance(to_v, datetime) else to_v
            changes_out[k] = {"from": from_v, "to": to_v}
        else:
            changes_out[k] = v

    return {
        "at": at_ms,
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes_out,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_employee_doc(doc: dict) -> dict:
    created_at = doc.get("created_at")
    created_at_ms = utc_dt_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
StrictEpochMillis = Annotated[
    int,
    Field(
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Instant as epoch milliseconds",
    ),
]

StatusEnum = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusEnum = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start")
        if shift_start is not None and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictEpochMillis] = None
    status: PresenceStatusEnum = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictEpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[StrictEpochMillis] = None
    punch_out: Optional[StrictEpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB answers a ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as e:
        logger.error("MongoDB ping failed: %s", e)
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforced by database unique index on emp_code."""
    doc = body.model_dump()
    doc["created_at"] = epoch_ms_to_utc_dt(now_utc_epoch_ms())

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees: sorted by emp_code ascending, paginated."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe uniqueness enforced by (emp_code, date) index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_utc_epoch_ms()
    punch_ms = truncate_ms_to_sec(punch_ms)
    punch_dt = epoch_ms_to_utc_dt(punch_ms)

    att_date = get_attendance_date(punch_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_ms, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes most recent open punch-in atomically."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at_ms = body.punched_at if body.punched_at is not None else now_utc_epoch_ms()
    punched_at_ms = truncate_ms_to_sec(punched_at_ms)
    punched_at_dt = epoch_ms_to_utc_dt(punched_at_ms)

    # Find the employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at_dt}},
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_ms = utc_dt_to_epoch_ms(record["punch_in"])
    if punched_at_ms <= punch_in_ms:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if punched_at_ms - punch_in_ms > 24 * 3600 * 1000:
        raise HTTPException(status_code=422, detail="punched_at cannot exceed 24 hours after punch_in")

    # Compute derived fields
    work_hrs = compute_work_hours(punch_in_ms, punched_at_ms)
    half_d = compute_half_day(work_hrs)
    ot_min = compute_overtime_minutes(punched_at_ms, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic state transition from punch_out == None to punched_out
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        # A concurrent punch-out beat us to it
        raise HTTPException(status_code=409, detail="Record was punched out by a concurrent request")

    return serialize_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusEnum] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records: sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", status_code=200, tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's attendance record with audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    old_doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old_doc is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else old_doc["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Cannot supply punch times when setting ABSENT or LEAVE")

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            cand_punch_in_ms = truncate_ms_to_sec(body.punch_in)
        else:
            old_punch_in_val = old_doc.get("punch_in")
            if old_punch_in_val is None:
                raise HTTPException(status_code=422, detail="Presence status requires a punch_in")
            cand_punch_in_ms = utc_dt_to_epoch_ms(old_punch_in_val)

        # Rule: punch_in must stay on the record's attendance date (R1)
        computed_date = get_attendance_date(cand_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not fall on the attendance date")

        new_punch_in_dt = epoch_ms_to_utc_dt(cand_punch_in_ms)

        # Candidate punch_out
        if body.punch_out is not None:
            cand_punch_out_ms = truncate_ms_to_sec(body.punch_out)
            new_punch_out_dt = epoch_ms_to_utc_dt(cand_punch_out_ms)
        else:
            old_punch_out_val = old_doc.get("punch_out")
            if old_punch_out_val is not None:
                cand_punch_out_ms = utc_dt_to_epoch_ms(old_punch_out_val)
                new_punch_out_dt = epoch_ms_to_utc_dt(cand_punch_out_ms)
            else:
                cand_punch_out_ms = None
                new_punch_out_dt = None

        # Rule: punch_out must be after punch_in and within 24 hours
        if new_punch_out_dt is not None:
            if cand_punch_out_ms <= cand_punch_in_ms:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if cand_punch_out_ms - cand_punch_in_ms > 24 * 3600 * 1000:
                raise HTTPException(status_code=422, detail="punch_out cannot exceed 24 hours after punch_in")

        # Recompute derived fields
        new_late_minutes = compute_late_minutes(cand_punch_in_ms, emp["shift_start"], emp["shift_end"], date)
        if new_punch_out_dt is not None:
            new_work_hours = compute_work_hours(cand_punch_in_ms, cand_punch_out_ms)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime_minutes(cand_punch_out_ms, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Determine changes
    old_punch_in_dt = old_doc.get("punch_in")
    if old_punch_in_dt is not None:
        old_punch_in_dt = epoch_ms_to_utc_dt(utc_dt_to_epoch_ms(old_punch_in_dt))

    old_punch_out_dt = old_doc.get("punch_out")
    if old_punch_out_dt is not None:
        old_punch_out_dt = epoch_ms_to_utc_dt(utc_dt_to_epoch_ms(old_punch_out_dt))

    old_late_minutes = old_doc.get("late_minutes", 0)
    old_overtime_minutes = old_doc.get("overtime_minutes", 0)
    old_half_day = old_doc.get("half_day", False)
    old_work_hours = old_doc.get("work_hours")

    changes: Dict[str, Any] = {}
    if old_doc["status"] != target_status:
        changes["status"] = {"from": old_doc["status"], "to": target_status}
    if old_punch_in_dt != new_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in_dt, "to": new_punch_in_dt}
    if old_punch_out_dt != new_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out_dt, "to": new_punch_out_dt}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changed nothing")

    now_dt = epoch_ms_to_utc_dt(now_utc_epoch_ms())
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update preserving concurrent history changes
    old_history_len = len(old_doc.get("history", []))
    if old_history_len == 0:
        match_condition = {
            "_id": old_doc["_id"],
            "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}],
        }
    else:
        match_condition = {
            "_id": old_doc["_id"],
            "history": {"$size": old_history_len},
        }

    updated = db.attendance_logs.find_one_and_update(
        match_condition,
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Concurrent regularization update conflict")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics (Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "isoDayOfWeek": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$isoDayOfWeek", 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_d = dt_date(year, m, 1)
    end_d = dt_date(year, m, last_day)

    joined_d = dt_date.fromisoformat(emp["joined_on"])
    if joined_d > end_d:
        working_days = 0
    else:
        eff_start = max(start_d, joined_d)
        working_days = sum(
            1 for day_offset in range((end_d - eff_start).days + 1)
            if (eff_start + timedelta(days=day_offset)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    present_days = round_half_up_2(present_days)
    if working_days > 0:
        pct = round_half_up_2((present_days / working_days) * 100)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "isoDayOfWeek": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$project": {"department": 1, "emp_code": 1, "logs": 1}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$logs.isoDayOfWeek", 5]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "wh_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "wh_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_count", 0]},
                        {"$divide": ["$wh_sum", "$wh_count"]},
                        None,
                    ]
                },
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    pipeline = build_department_summary_pipeline(month, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        avg_wh = round_half_up_2(d.get("avg_work_hours"))
        items.append({
            "department": d["department"],
            "headcount": int(d["headcount"]),
            "present_days": round_half_up_2(float(d.get("present_days", 0.0))),
            "avg_work_hours": avg_wh,
            "late_count": int(d.get("late_count", 0)),
            "total_late_minutes": int(d.get("total_late_minutes", 0)),
            "leave_count": int(d.get("leave_count", 0)),
            "on_duty_count": int(d.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month using standard competition ranking."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str, dept_emp_codes: List[str]) -> List[Dict[str, Any]]:
    f_year, f_month, f_day = map(int, from_date.split("-"))
    t_year, t_month, t_day = map(int, to_date.split("-"))
    from_dt = datetime(f_year, f_month, f_day, tzinfo=UTC)
    to_dt_plus_1 = datetime(t_year, t_month, t_day, tzinfo=UTC) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "as_date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "as_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$as_date",
                        "timezone": "UTC",  # Pin to UTC so date strings are
                        # correct regardless of MongoDB server locale.
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "isoDayOfWeek": {"$isoDayOfWeek": "$as_date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$lte": ["$isoDayOfWeek", 5]},
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        # Raw quotient — Python's round_half_up_4() applies the
                        # authoritative half-up rounding (R8). MongoDB $round uses
                        # Banker's rounding and must not be used here.
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """Daily attendance trend for a department with gap-filling and 7-day moving average."""
    # Check department exists
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    from_d = dt_date.fromisoformat(from_date)
    to_d = dt_date.fromisoformat(to_date)
    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    diff_days = (to_d - from_d).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        att_rate = round_half_up_4(d.get("attendance_rate"))
        m_avg = round_half_up_4(d.get("moving_avg_7d"))
        items.append({
            "date": d["date"],
            "is_working_day": bool(d["is_working_day"]),
            "headcount": int(d["headcount"]),
            "present_count": round_half_up_2(float(d.get("present_count", 0.0))),
            "late_count": int(d.get("late_count", 0)),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: MongoDB Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusEnum] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """MongoDB explain for an endpoint's main query with executionStats verbosity."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        explain_res = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }, verbosity="executionStats")
        coll = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly explain")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        coll = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")

        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command("explain", {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        coll = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        coll = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend explain")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' query parameters are required for department_trend explain")

        from_d = dt_date.fromisoformat(from_date)
        to_d = dt_date.fromisoformat(to_date)
        if to_d < from_d:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }, verbosity="executionStats")
        coll = "attendance_logs"

    clean_explain = json.loads(json_util.dumps(explain_res))
    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": clean_explain,
    }

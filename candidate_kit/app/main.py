"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded with override=False so system env vars win)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables must win over local .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # Check connectivity first before creating indexes
        client.admin.command("ping")
        # employees
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1), ("department", 1)])

        # attendance_logs
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
        db.attendance_logs.create_index([("date", 1)])
    except Exception:
        # Ignore startup connection errors so /health can report 503 if DB is unreachable
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], places: int = 2) -> Optional[float]:
    """R8 Rounding: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON / Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def current_epoch_ms() -> int:
    """Current UTC time in epoch milliseconds truncated to whole seconds."""
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    return int(now_dt.timestamp()) * 1000


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, record_date_str: str) -> int:
    """
    R2 Late: Grace period is 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) elapsed since shift_start.
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    rec_d = date.fromisoformat(record_date_str)
    shift_start_ist = datetime.combine(rec_d, time(start_h, start_m), tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    elapsed_seconds = (punch_in_utc - shift_start_utc).total_seconds()
    if elapsed_seconds > 600:  # strictly more than 10 minutes
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    val = Decimal(seconds) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, record_date_str: str) -> int:
    """
    R3 Overtime: Whole minutes (floor) from shift_end to punch_out, counted only if that is
    at least 30; otherwise 0.
    """
    rec_d = date.fromisoformat(record_date_str)
    is_overnight = shift_end <= shift_start
    shift_end_d = rec_d + timedelta(days=1) if is_overnight else rec_d
    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_ist = datetime.combine(shift_end_d, time(end_h, end_m), tzinfo=IST)
    shift_end_utc = shift_end_ist.astimezone(timezone.utc)

    diff_seconds = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_seconds >= 30 * 60:
        return int(diff_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day: After punch-out, half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def get_working_days_for_month(month_str: str, joined_on_str: str) -> int:
    """
    R7 Working days: Mon-Fri, counted from joined_on for mid-month joiners.
    """
    y, m = map(int, month_str.split("-"))
    first_day = date(y, m, 1)
    last_day_num = calendar.monthrange(y, m)[1]
    last_day = date(y, m, last_day_num)

    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > last_day:
        return 0

    start_date = max(first_day, joined_on)
    count = 0
    curr = start_date
    while curr <= last_day:
        if curr.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        curr += timedelta(days=1)
    return count


def format_employee_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    created_at_ms = datetime_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_out = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
        changes_out = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = datetime_to_epoch_ms(f_val) if isinstance(f_val, datetime) else f_val
                t_val = datetime_to_epoch_ms(t_val) if isinstance(t_val, datetime) else t_val
            changes_out[k] = {"from": f_val, "to": t_val}
        history_out.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


def make_json_serializable(obj: Any) -> Any:
    """Recursively converts BSON ObjectId, Datetime, etc. for JSON serialization."""
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, "__str__") and type(obj).__name__ == "ObjectId":
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Unique emp_code enforced by unique database index.
    created_at is set by the server as an instant (stored as UTC BSON date, returned as epoch ms).
    """
    doc = body.model_dump()
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee_record(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Sorted by emp_code ascending. total counts the filtered set, not the whole collection."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Creates the day's record. Concurrency: atomic insert backed by unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_in_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    punch_in_utc = epoch_ms_to_datetime(punch_in_ms)

    att_date = get_attendance_date(punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_minutes = compute_late_minutes(punch_in_utc, emp.get("shift_start", "09:30"), att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Computes work_hours, overtime_minutes, and half_day.
    Two simultaneous punch-outs yield exactly one 200 and the other 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    punch_out_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    punch_out_utc = epoch_ms_to_datetime(punch_out_ms)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(
        punch_out_utc,
        emp.get("shift_start", "09:30"),
        emp.get("shift_end", "18:30"),
        record["date"],
    )

    # Atomic update ensuring two simultaneous punch-outs yield exactly one 200 and one 409
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "that record is already punched out")

    return format_attendance_record(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Sorted by date descending, then emp_code ascending.
    Must stay fast on ~100,000 records; does not load collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends exactly one entry to history without losing audit trail.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for this date")

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE status clears punch times; punch_in/punch_out cannot be supplied")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # A presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        computed_date = get_attendance_date(
            final_punch_in,
            emp.get("shift_start", "09:30"),
            emp.get("shift_end", "18:30"),
        )
        if computed_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        # punch_out
        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        # If punch_out exists, it must be after punch_in and within 24 hours
        if final_punch_out is not None:
            diff = (final_punch_out - final_punch_in).total_seconds()
            if diff <= 0 or diff > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = compute_half_day(final_work_hours)
            final_overtime_minutes = compute_overtime(
                final_punch_out,
                emp.get("shift_start", "09:30"),
                emp.get("shift_end", "18:30"),
                date,
            )
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

        final_late_minutes = compute_late_minutes(final_punch_in, emp.get("shift_start", "09:30"), date)

    # Determine changes
    db_changes: Dict[str, Any] = {}
    if record.get("status") != new_status:
        db_changes["status"] = {"from": record.get("status"), "to": new_status}

    old_pi = record.get("punch_in")
    if old_pi != final_punch_in:
        db_changes["punch_in"] = {"from": old_pi, "to": final_punch_in}

    old_po = record.get("punch_out")
    if old_po != final_punch_out:
        db_changes["punch_out"] = {"from": old_po, "to": final_punch_out}

    old_wh = record.get("work_hours")
    if old_wh != final_work_hours:
        db_changes["work_hours"] = {"from": old_wh, "to": final_work_hours}

    old_lm = record.get("late_minutes", 0)
    if old_lm != final_late_minutes:
        db_changes["late_minutes"] = {"from": old_lm, "to": final_late_minutes}

    old_om = record.get("overtime_minutes", 0)
    if old_om != final_overtime_minutes:
        db_changes["overtime_minutes"] = {"from": old_om, "to": final_overtime_minutes}

    old_hd = record.get("half_day", False)
    if old_hd != final_half_day:
        db_changes["half_day"] = {"from": old_hd, "to": final_half_day}

    # A request that changes nothing is 422
    if not db_changes:
        raise HTTPException(422, "A request that changes nothing is 422")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    # Optimistic locking using history length to prevent lost updates under concurrency
    history_len = len(record.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "Concurrent modification conflict")

    return format_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_d:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$project": {
                "is_mon_fri": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "half_day": "$half_day",
                "late_minutes": "$late_minutes",
                "overtime_minutes": "$overtime_minutes",
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_mon_fri", "$is_present"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = get_working_days_for_month(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_d:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    pipeline: List[Dict[str, Any]] = [
        {"$match": emp_match},
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
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                    {
                        "$project": {
                            "present_val": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                            "is_late": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                            "late_minutes": "$late_minutes",
                            "is_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                            "is_on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                            "valid_work_hour": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$work_hours", None]},
                                        ]
                                    },
                                    "$work_hours",
                                    None,
                                ]
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$sum": "$logs.present_val"}},
                "late_count": {"$sum": {"$sum": "$logs.is_late"}},
                "total_late_minutes": {"$sum": {"$sum": "$logs.late_minutes"}},
                "leave_count": {"$sum": {"$sum": "$logs.is_leave"}},
                "on_duty_count": {"$sum": {"$sum": "$logs.is_on_duty"}},
                "all_wh_nested": {
                    "$push": {
                        "$filter": {
                            "input": "$logs.valid_work_hour",
                            "as": "wh",
                            "cond": {"$ne": ["$$wh", None]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "all_wh": {
                    "$reduce": {
                        "input": "$all_wh_nested",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$all_wh"}, 0]},
                        {"$avg": "$all_wh"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    Department summary for a month across employees and attendance_logs.
    Includes headcount for employees with zero logs; omits departments with 0 headcount.
    """
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for r in rows:
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_d:02d}"

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
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    Late-comers leaderboard for a month using standard competition ranking ($rank).
    limit is applied after ranking; rows are ordered by total_late_minutes desc, then emp_code asc.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, from_str: str, to_str: str
) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    from_dt = datetime.combine(from_d, time.min, tzinfo=timezone.utc)
    to_bound_dt = datetime.combine(to_d + timedelta(days=1), time.min, tzinfo=timezone.utc)

    pipeline: List[Dict[str, Any]] = [
        # Match attendance logs within date range
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
                "date_str": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with a guaranteed seed row from employees for from_str to ensure densify has an entry
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "d": {"$literal": from_dt},
                            "date_str": {"$literal": from_str},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$d",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_bound_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "d": "$_id",
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": {"date": "$_id", "timezone": "UTC"}},
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
                    {"$count": "total"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "d": 1,
                "date": 1,
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$first": "$hc.total"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "d": 1,
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
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
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
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department.
    Returns exactly one row per calendar day from 'from' to 'to' inclusive via gap-filling ($densify).
    """
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(422, "'to' date must be on or after 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "Date range exceeds maximum of 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
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
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation of the named endpoint.
    Accepts the same query parameters as the endpoint it explains.
    Missing required parameters -> 422.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        explain_doc = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": [("date", -1), ("emp_code", 1)],
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": make_json_serializable(explain_doc),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_doc = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": make_json_serializable(explain_doc),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": make_json_serializable(explain_doc),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_doc = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": make_json_serializable(explain_doc),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days + 1 > 92:
            raise HTTPException(422, "Invalid date range for department_trend")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_doc = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": make_json_serializable(explain_doc),
        }

    raise HTTPException(422, "Unsupported explain endpoint")

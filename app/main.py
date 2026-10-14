"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

All endpoints of the contract are implemented in this single file.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# load_dotenv() does NOT override variables that already exist in the environment,
# so the grader's real MONGO_URI / MONGO_DB win over a local .env.
load_dotenv()

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones,
# so we never mix naive and aware values in arithmetic.
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent - create_index with the same spec is a no-op)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # Guarantees: one employee per emp_code; one attendance record per (emp_code, date).
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # GET /employees?department=...  (filter + sort by emp_code)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # GET /attendance sorted by date desc, emp_code asc (with or without a date range)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # GET /attendance?status=...  (equality filter, then the same sort)
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    # department summary / trend start from employees: filter by joined_on (R9)
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # POST /attendance/punch-out: "latest record of this employee with punch_in <= t"
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health will report 503 until MongoDB is reachable.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds. A naive datetime is assumed to be UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hhmm(shift_end) <= _hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts started the previous day
    if the punch-in is earlier than shift_end."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < _hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Aware IST start and end of the shift that belongs to attendance date `date_str`."""
    d = date_cls.fromisoformat(date_str)
    s, e = _hhmm(shift_start), _hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, s.hour, s.minute, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, e.hour, e.minute, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Business rules R2 - R5 (all take aware datetimes already truncated to seconds)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    start, _ = shift_window(date_str, shift_start, shift_end)
    secs = int((punch_in - start).total_seconds())
    # strictly more than 10:00 after start; then whole minutes since shift_start
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    # Decimal + ROUND_HALF_UP: Python's round() is banker's rounding on a binary float.
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    _, end = shift_window(date_str, shift_start, shift_end)
    secs = int((punch_out - end).total_seconds())
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
# strict=True rejects floats, strings and bools; the bounds reject seconds-looking values.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


def _valid_date(v: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError("date must be YYYY-MM-DD")
    date_cls.fromisoformat(v)  # rejects things like 2026-02-30
    return v


DateStr = Annotated[str, AfterValidator(_valid_date)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        return _valid_date(v)

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, late_minutes, ...) are not declared, so a client that
    # sends them is silently ignored (pydantic drops unknown fields by default).
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API JSON). _id never leaves the server.
# --------------------------------------------------------------------------- #
def _ms_or_none(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def record_out(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:  # legacy records may omit history
        history.append(
            {
                "at": dt_to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": {
                    field: {"from": _ms_or_none(c.get("from")), "to": _ms_or_none(c.get("to"))}
                    for field, c in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),  # legacy records may omit half_day
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        # The unique index decides; no check-then-insert race.
        db.employees.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Unique index on (emp_code, date): of N simultaneous requests exactly one insert wins.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


ATTENDANCE_SORT = {"date": DESCENDING, "emp_code": ASCENDING}


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    """The filter used by GET /attendance (and by its explain)."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
MAX_SHIFT = timedelta(hours=24)


def derived_for_close(date_str: str, emp: dict, pin: datetime, pout: Optional[datetime]) -> dict:
    """work_hours / overtime / half_day for a record with these punch times (R3-R5)."""
    if pout is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": wh,
        "overtime_minutes": compute_overtime(pout, date_str, emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(wh),
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # Most recent record whose punch_in is at or before the punch-out instant. Using the
    # punch instant (not the calendar date) is what makes overnight shifts work.
    # ABSENT/LEAVE records have a null punch_in and never match $lte against a date.
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= doc["punch_in"] or ts - doc["punch_in"] > MAX_SHIFT:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    changes = {"punch_out": ts, **derived_for_close(doc["date"], emp, doc["punch_in"], ts)}
    # Atomic compare-and-set: only the request that still sees punch_out == null wins.
    # (null in a filter also matches a missing field.) Everyone else gets None -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None}, {"$set": changes}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = doc["status"]
    old_pin, old_pout = doc.get("punch_in"), doc.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_pin = new_pout = None  # such a status clears both punch times
    else:
        new_pin = ms_to_dt(body.punch_in) if body.punch_in is not None else old_pin
        new_pout = ms_to_dt(body.punch_out) if body.punch_out is not None else old_pout
        if new_pin is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_pout is not None and (new_pout <= new_pin or new_pout - new_pin > MAX_SHIFT):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_pin, "punch_out": new_pout}
    if new_pin is None:
        new["late_minutes"] = 0
    else:
        new["late_minutes"] = compute_late_minutes(new_pin, date, emp["shift_start"], emp["shift_end"])
    new.update(derived_for_close(date, emp, new_pin, new_pout))

    old = {
        "status": old_status,
        "punch_in": old_pin,
        "punch_out": old_pout,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }
    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(doc.get("history") or [])
    # Optimistic concurrency: the write only applies if the record is still exactly what we
    # read (same punch times / status and the same number of history entries). A concurrent
    # PATCH or punch-out in between makes this match nothing -> 409, so no history entry is
    # lost and no stale derived value is written.
    cond: dict = {
        "_id": doc["_id"],
        "status": old_status,
        "punch_in": old_pin,
        "punch_out": old_pout,
    }
    if n == 0:
        cond["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        cond["history"] = {"$size": n}
    updated = db.attendance_logs.find_one_and_update(
        cond, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics. Every number comes out of a MongoDB aggregation over the STORED derived
# values (late_minutes, overtime_minutes, half_day, work_hours); nothing is recomputed from
# punch times. Python only validates input, does calendar arithmetic that has no document
# behind it (working days) and applies half-up rounding (MongoDB's $round is banker's).
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
MAX_TREND_DAYS = 92


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'). Dates are YYYY-MM-DD strings, which sort
    lexicographically in date order, so plain $gte/$lte on them is correct and indexable."""
    if not re.fullmatch(MONTH_RE, month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, mth = int(month[:4]), int(month[5:])
    if y < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    return f"{month}-01", f"{month}-{calendar.monthrange(y, mth)[1]:02d}"


def count_working_days(first: str, last: str) -> int:
    """Mon-Fri days in [first, last] inclusive (R7). No holiday calendar."""
    d, end, n = date_cls.fromisoformat(first), date_cls.fromisoformat(last), 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def dec_round(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ---- small expression helpers shared by the pipelines ---------------------- #
def _is_present(prefix: str) -> dict:
    return {"$in": [prefix + "status", list(PRESENCE)]}


def _late(prefix: str) -> dict:
    return {"$ifNull": [prefix + "late_minutes", 0]}


def _is_late(prefix: str) -> dict:
    return {"$cond": [{"$gt": [_late(prefix), 0]}, 1, 0]}


def _is_weekday(date_path: str) -> dict:
    """Mon-Fri for a 'YYYY-MM-DD' string field ($dayOfWeek: 1 = Sunday ... 7 = Saturday)."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": date_path, "format": "%Y-%m-%d"}}}
    return {"$and": [{"$eq": [{"$type": date_path}, "string"]}, {"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _present_day_value(prefix: str) -> dict:
    """1 for a present weekday, 0.5 for a present half-day weekday, else 0 (R5-R7)."""
    return {
        "$cond": [
            {"$and": [_is_present(prefix), _is_weekday(prefix + "date")]},
            {"$cond": [{"$ifNull": [prefix + "half_day", False]}, 0.5, 1]},
            0,
        ]
    }


# ---- pipeline builders (the endpoints and /admin/explain both call these) --- #
def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        # uses the unique (emp_code, date) index: equality on emp_code + range on date
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_day_value("$")},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _is_late("$")},
                "total_late_minutes": {"$sum": _late("$")},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9: joined on/before the end of the month
    if department is not None:
        emp_match["department"] = department
    lg = "$logs."
    has_wh = {"$and": [_is_present(lg), {"$isNumber": lg + "work_hours"}]}
    return [
        # Start from EMPLOYEES, not logs: people with zero logs must still be counted.
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
                "as": "logs",
            }
        },
        # preserveNull...: an employee with no logs survives as one row with no 'logs'
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # step 1: one row per employee
        {
            "$group": {
                "_id": {"d": "$department", "e": "$emp_code"},
                "present_days": {"$sum": _present_day_value(lg)},
                # work hours kept as integer hundredths so the final mean is exact
                "wh_hundredths": {
                    "$sum": {"$cond": [has_wh, {"$round": [{"$multiply": [lg + "work_hours", 100]}, 0]}, 0]}
                },
                "wh_n": {"$sum": {"$cond": [has_wh, 1, 0]}},
                "late_count": {"$sum": _is_late(lg)},
                "total_late_minutes": {"$sum": _late(lg)},
                "leave_count": {"$sum": {"$cond": [{"$eq": [lg + "status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": [lg + "status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        # step 2: one row per department. Summing the sums and counts (not averaging
        # per-employee averages) makes avg_work_hours an average over records.
        {
            "$group": {
                "_id": "$_id.d",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "wh_hundredths": {"$sum": "$wh_hundredths"},
                "wh_n": {"$sum": "$wh_n"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pl: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},  # (date, emp_code) index
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": _late("$")},
                "late_count": {"$sum": _is_late("$")},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        # logs whose employee does not exist have an empty 'emp' and are dropped here
        {"$unwind": "$emp"},
    ]
    if department is not None:  # ranking happens inside the department only
        pl.append({"$match": {"emp.department": department}})
    pl += [
        # $rank = standard competition ranking (1, 2, 2, 4): ties share a rank, next is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit is applied AFTER ranking, on the rank itself, so ties at the cutoff all stay
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
    ]
    return pl


def trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    lo = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    lg = "$logs."
    dow = {"$dayOfWeek": "$d"}
    window = {"documents": [-6, 0]}  # current row + up to 6 before it, all inside the range
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": d_from, "$lte": d_to}}}],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        # per day: present half-units (full day = 2, half day = 1) and late count
        {
            "$group": {
                "_id": "$logs.date",
                "P2": {
                    "$sum": {
                        "$cond": [_is_present(lg), {"$cond": [{"$ifNull": [lg + "half_day", False]}, 1, 2]}, 0]
                    }
                },
                "late_count": {"$sum": _is_late(lg)},
            }
        },
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one row for EVERY calendar day in [from, to]
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {
            "$addFields": {
                "ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                "wd": {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]},
                "P2": {"$ifNull": ["$P2", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # R9 headcount for that day: department employees with joined_on <= the day
        {
            "$lookup": {
                "from": "employees",
                "let": {"ds": "$ds"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$ds"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$addFields": {"H": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # attendance_rate in integer ten-thousandths, half-up, using exact integer arithmetic:
        # rate = (P2/2)/H, so rate*10000 = P2*5000/H and half-up = floor((P2*10000 + H) / (2H)).
        # Only on working days with headcount > 0; otherwise null.
        {
            "$addFields": {
                "k": {
                    "$cond": [
                        {"$and": ["$wd", {"$gt": ["$H", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$P2", 10000]}, "$H"]},
                                    {"$multiply": [2, "$H"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$addFields": {"has_k": {"$cond": [{"$eq": ["$k", None]}, 0, 1]}}},
        # 7-day moving average over the REPORTED (rounded) rates; null rates are skipped
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "sum_k": {"$sum": "$k", "window": window},
                    "n_k": {"$sum": "$has_k", "window": window},
                },
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$ds",
                "is_working_day": "$wd",
                "headcount": "$H",
                "present_count": {"$divide": ["$P2", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$k", None]}, None, {"$divide": ["$k", 10000]}]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$n_k", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$sum_k", 2]}, "$n_k"]},
                                            {"$multiply": ["$n_k", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
    ]


def check_trend_range(d_from: str, d_to: str) -> None:
    f, t = date_cls.fromisoformat(d_from), date_cls.fromisoformat(d_to)
    if t < f:
        raise HTTPException(422, "to must be >= from")
    if (t - f).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")


# ---- endpoints -------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)))
    agg = rows[0] if rows else {}
    present = Decimal(str(agg.get("present_days", 0)))
    # R7: working days count from joined_on for a mid-month joiner (0 if they join later)
    first = max(start, emp["joined_on"])
    working = count_working_days(first, end) if first <= end else 0
    pct = dec_round(present / Decimal(working) * 100, 2) if working else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": dec_round(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(start, end, department)):
        n = int(r["wh_n"])
        avg = dec_round(Decimal(int(r["wh_hundredths"])) / Decimal(n) / 100, 2) if n else None
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": dec_round(Decimal(str(r["present_days"])), 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    items = []
    for r in db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)):
        items.append(
            {
                "rank": int(r["rank"]),
                "emp_code": r["emp_code"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": int(r["total_late_minutes"]),
                "late_count": int(r["late_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: DateStr = Query(alias="from"),
    to: DateStr = Query(),
):
    check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = []
    for r in db.employees.aggregate(trend_pipeline(department, from_, to)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": r["attendance_rate"],
                "moving_avg_7d": r["moving_avg_7d"],
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain: runs MongoDB's explain (executionStats) on the SAME query/pipeline the
# real endpoint builds, using the same builder functions above.
# --------------------------------------------------------------------------- #
def _required(values: dict) -> None:
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[DateStr] = Query(None, alias="from"),
    to: Optional[DateStr] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        cmd = {
            "find": collection,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _required({"emp_code": emp_code, "month": month})
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            _required({"month": month})
            collection, pipeline = "employees", summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            _required({"month": month})
            collection, pipeline = "attendance_logs", leaderboard_pipeline(*month_bounds(month), limit, department)
        else:  # department_trend
            _required({"department": department, "from": from_, "to": to})
            check_trend_range(from_, to)
            collection, pipeline = "employees", trend_pipeline(department, from_, to)
        cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    try:
        result = db.command({"explain": cmd, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    # explain output contains BSON types (Timestamp, ObjectId, Int64...); make it plain JSON
    explain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
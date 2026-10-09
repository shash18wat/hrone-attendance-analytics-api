"""Employee Attendance & Analytics API for the HROne take-home assignment."""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError


load_dotenv(override=False)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
MAX_EPOCH_MS = 4_102_444_800_000

mongo_client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=2_000,
)
db = mongo_client[os.getenv("MONGO_DB", "attendance_db")]
_indexes_ready = False


def ensure_indexes() -> None:
    """Create the indexes used by writes, list endpoints, and analytics."""
    global _indexes_ready
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="emp_code_unique"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_emp_code_idx",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="joined_department_idx",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined_idx",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_code_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_emp_code_sort_idx",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("status", ASCENDING), ("date", DESCENDING)],
        name="emp_status_date_idx",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="status_date_emp_idx",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_punch_in_idx",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="date_late_emp_idx",
    )
    _indexes_ready = True


def require_indexes() -> None:
    """Retry index setup if MongoDB became available after app startup."""
    if _indexes_ready:
        return
    try:
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB indexes are unavailable") from exc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        mongo_client.admin.command("ping")
        ensure_indexes()
    except ServerSelectionTimeoutError:
        # Keep the HTTP server available so /health can report 503 while MongoDB
        # is unavailable. Writes rely on the indexes once MongoDB is reachable.
        logger.warning("MongoDB is unavailable during startup; index creation deferred")
    yield
    mongo_client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


StatusValue = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=MAX_EPOCH_MS)]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_employee_dates_and_shift(self) -> "EmployeeIn":
        try:
            if Date.fromisoformat(self.joined_on).isoformat() != self.joined_on:
                raise ValueError
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: StatusValue = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Time, rounding, and serialization helpers
# ---------------------------------------------------------------------------
def utc_datetime(value: datetime) -> datetime:
    """Treat legacy naive PyMongo values as UTC and return an aware UTC value."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Convert epoch milliseconds to UTC, truncating to a whole second (R1)."""
    instant = EPOCH + timedelta(milliseconds=value)
    return instant.replace(microsecond=0)


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = utc_datetime(value) - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def current_utc_datetime() -> datetime:
    """Use UTC and millisecond precision for a server-generated instant."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1_000) * 1_000)


def month_bounds(month: str) -> tuple[str, str, Date, Date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    year, month_number = map(int, month.split("-"))
    try:
        start = Date(year, month_number, 1)
        end = Date(year, month_number, calendar.monthrange(year, month_number)[1])
    except ValueError as exc:
        raise HTTPException(422, "month must be in YYYY-MM format") from exc
    return start.isoformat(), end.isoformat(), start, end


def iso_date(value: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise HTTPException(422, "date must be a valid YYYY-MM-DD date") from exc


def clock_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    local = utc_datetime(punch_in).astimezone(IST)
    day = local.date()
    if clock_time(shift_end) <= clock_time(shift_start) and local.time().replace(tzinfo=None) < clock_time(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_instant(attendance_date: str, shift_start: str) -> datetime:
    day = iso_date(attendance_date)
    return datetime.combine(day, clock_time(shift_start), tzinfo=IST).astimezone(UTC)


def shift_end_instant(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    day = iso_date(attendance_date)
    if clock_time(shift_end) <= clock_time(shift_start):
        day += timedelta(days=1)
    return datetime.combine(day, clock_time(shift_end), tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, attendance_date_str: str, shift_start: str) -> int:
    elapsed_seconds = (utc_datetime(punch_in) - shift_start_instant(attendance_date_str, shift_start)).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return max(0, int(elapsed_seconds // 60))


def round_half_up(value: Decimal | float | int, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((utc_datetime(punch_out) - utc_datetime(punch_in)).total_seconds()))
    return float((seconds / Decimal(3_600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(
    punch_out: datetime,
    attendance_date_str: str,
    shift_start: str,
    shift_end: str,
) -> int:
    extra_seconds = (utc_datetime(punch_out) - shift_end_instant(attendance_date_str, shift_start, shift_end)).total_seconds()
    if extra_seconds < 1_800:
        return 0
    return max(0, int(extra_seconds // 60))


def derived_values(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    attendance_date_str: str,
    employee: dict,
) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    late = compute_late_minutes(punch_in, attendance_date_str, employee["shift_start"]) if punch_in else 0
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}

    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(
            punch_out,
            attendance_date_str,
            employee["shift_start"],
            employee["shift_end"],
        ),
        "half_day": hours < 4.50,
    }


def api_employee(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["created_at"] = datetime_to_epoch_ms(result.get("created_at"))
    return result


def api_attendance(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    for field in ("punch_in", "punch_out"):
        result[field] = datetime_to_epoch_ms(result.get(field))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    history = []
    for entry in result["history"]:
        item = dict(entry)
        item["at"] = datetime_to_epoch_ms(item.get("at"))
        changes = {}
        for field, change in item.get("changes", {}).items():
            values = dict(change)
            if field in ("punch_in", "punch_out"):
                values["from"] = datetime_to_epoch_ms(values.get("from"))
                values["to"] = datetime_to_epoch_ms(values.get("to"))
            changes[field] = values
        item["changes"] = changes
        history.append(item)
    result["history"] = history
    return result


def weekday_count(start: Date, end: Date) -> int:
    """Count Monday-Friday days inclusively without walking every calendar day."""
    if start > end:
        return 0
    day_count = (end - start).days + 1
    full_weeks, remainder = divmod(day_count, 7)
    weekdays = full_weeks * 5
    weekdays += sum(1 for offset in range(remainder) if (start.weekday() + offset) % 7 < 5)
    return weekdays


def pipeline_round(value: object, places: int) -> float | None:
    if value is None:
        return None
    return float(round_half_up(value, places))


# ---------------------------------------------------------------------------
# Aggregation pipeline builders (also used by /admin/explain)
# ---------------------------------------------------------------------------
def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list[dict]:
    present_on_weekday = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$gte": [{"$dayOfWeek": "$_attendance_date"}, 2]},
            {"$lte": [{"$dayOfWeek": "$_attendance_date"}, 6]},
        ]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$addFields": {
                "_attendance_date": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            present_on_weekday,
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(start: str, end: str, department: str | None) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department

    present_on_weekday = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$gte": [{"$dayOfWeek": "$_attendance_date"}, 2]},
            {"$lte": [{"$dayOfWeek": "$_attendance_date"}, 6]},
        ]
    }
    worked_record = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }
    empty_stats = {
        "present_days": 0.0,
        "work_hours_sum": 0.0,
        "work_hours_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_attendance_date": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$emp_code",
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        present_on_weekday,
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {"$cond": [worked_record, "$work_hours", 0.0]}
                            },
                            "work_hours_count": {
                                "$sum": {"$cond": [worked_record, 1, 0]}
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "_employee_stats",
            }
        },
        {
            "$set": {
                "_employee_stats": {
                    "$ifNull": [{"$arrayElemAt": ["$_employee_stats", 0]}, empty_stats]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_employee_stats.present_days"},
                "work_hours_sum": {"$sum": "$_employee_stats.work_hours_sum"},
                "work_hours_count": {"$sum": "$_employee_stats.work_hours_count"},
                "late_count": {"$sum": "$_employee_stats.late_count"},
                "total_late_minutes": {"$sum": "$_employee_stats.total_late_minutes"},
                "leave_count": {"$sum": "$_employee_stats.leave_count"},
                "on_duty_count": {"$sum": "$_employee_stats.on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, department: str | None, limit: int) -> list[dict]:
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": start, "$lte": end},
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
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str, from_date: Date, to_date: Date
) -> list[dict]:
    start_text = from_date.isoformat()
    end_text = to_date.isoformat()
    start_instant = datetime.combine(from_date, time.min, tzinfo=UTC)
    end_instant = datetime.combine(to_date, time.min, tzinfo=UTC)
    date_text_expr = {
        "$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}
    }
    day_stats = {
        "$filter": {
            "input": "$_daily_stats",
            "as": "stat",
            "cond": {"$eq": ["$$stat.date", date_text_expr]},
        }
    }
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "_employee_codes": {"$push": "$emp_code"},
                "_join_dates": {"$push": "$joined_on"},
            }
        },
        {
            "$set": {
                "_days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_instant,
                                                "endDate": end_instant,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_instant,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_codes": "$_employee_codes"},
                "pipeline": [
                    {"$match": {"date": {"$gte": start_text, "$lte": end_text}}},
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$employee_codes"]}}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
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
                            "date": "$_id",
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
                "as": "_daily_stats",
            }
        },
        {
            "$set": {
                "_rows": {
                    "$map": {
                        "input": "$_days",
                        "as": "day",
                        "in": {
                            "date": "$$day",
                            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$$day"}, 5]},
                            "headcount": {
                                "$size": {
                                    "$filter": {
                                        "input": "$_join_dates",
                                        "as": "joined",
                                        "cond": {"$lte": ["$$joined", date_text_expr]},
                                    }
                                }
                            },
                            "present_count": {
                                "$sum": {
                                    "$map": {
                                        "input": day_stats,
                                        "as": "stat",
                                        "in": "$$stat.present_count",
                                    }
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$map": {
                                        "input": day_stats,
                                        "as": "stat",
                                        "in": "$$stat.late_count",
                                    }
                                }
                            },
                        },
                    }
                }
            }
        },
        {"$unwind": "$_rows"},
        {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$$ROOT", "$_rows"]}}},
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$group": {
                "_id": "$_id",
                "items": {
                    "$push": {
                        "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                        "is_working_day": "$is_working_day",
                        "headcount": "$headcount",
                        "present_count": "$present_count",
                        "late_count": "$late_count",
                        "attendance_rate": "$attendance_rate",
                        "moving_avg_7d": "$moving_avg_7d",
                    }
                },
            }
        },
        {"$project": {"_id": 0, "department": "$_id", "items": 1}},
    ]


def selected_attendance_index(
    emp_code: str | None, status: str | None
) -> dict[str, int]:
    if emp_code is not None and status is not None:
        return {"emp_code": ASCENDING, "status": ASCENDING, "date": DESCENDING}
    if status is not None:
        return {"status": ASCENDING, "date": DESCENDING, "emp_code": ASCENDING}
    if emp_code is not None:
        return {"emp_code": ASCENDING, "date": ASCENDING}
    return {"date": DESCENDING, "emp_code": ASCENDING}


def run_explain(collection_name: str, pipeline: list[dict], hint: dict) -> dict:
    return db.command(
        "explain",
        {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
            "hint": hint,
        },
        verbosity="executionStats",
    )


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    try:
        mongo_client.admin.command("ping")
        require_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    require_indexes()
    document = body.model_dump()
    document["created_at"] = current_utc_datetime()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return api_employee(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    require_indexes()
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    documents = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size
    ).limit(page_size)
    return {
        "items": [api_employee(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    require_indexes()
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_day(punched_at, employee["shift_start"], employee["shift_end"]).isoformat()
    document = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, day, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this attendance date") from exc
    return api_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict:
    require_indexes()
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$type": "date", "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")

    punch_in_at = utc_datetime(record["punch_in"])
    seconds_worked = (punched_at - punch_in_at).total_seconds()
    if seconds_worked <= 0 or seconds_worked > 86_400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    values = derived_values(
        record["status"], punch_in_at, punched_at, record["date"], employee
    )
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **values}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: StatusValue | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    return attendance_page(emp_code, date_from, date_to, status, page, page_size)


def attendance_query(
    emp_code: str | None,
    date_from: Date | None,
    date_to: Date | None,
    status: str | None,
) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_filter = {}
        if date_from is not None:
            date_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter
    if status is not None:
        query["status"] = status
    return query


def attendance_page(
    emp_code: str | None,
    date_from: Date | None,
    date_to: Date | None,
    status: str | None,
    page: int,
    page_size: int,
) -> dict:
    require_indexes()
    query = attendance_query(emp_code, date_from, date_to, status)
    hint = selected_attendance_index(emp_code, status)
    total = db.attendance_logs.count_documents(query, hint=hint)
    documents = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [api_attendance(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Date, body: RegularizeIn) -> dict:
    require_indexes()
    day = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else current["status"]
    if final_status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "ABSENT and LEAVE corrections cannot include punch times")
    final_punch_in = (
        epoch_ms_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else current.get("punch_in")
    )
    final_punch_out = (
        epoch_ms_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else current.get("punch_out")
    )
    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in = None
        final_punch_out = None
    elif final_punch_in is None:
        raise HTTPException(422, "a presence status requires punch_in")

    # A correction cannot silently move the record to another attendance day.
    if final_punch_in is not None:
        if attendance_day(final_punch_in, employee["shift_start"], employee["shift_end"]).isoformat() != day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        elapsed = (utc_datetime(final_punch_out) - utc_datetime(final_punch_in)).total_seconds()
        if elapsed <= 0 or elapsed > 86_400:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    input_changes = (
        final_status != current["status"]
        or final_punch_in != current.get("punch_in")
        or final_punch_out != current.get("punch_out")
    )
    if not input_changes:
        raise HTTPException(422, "correction does not change the record")

    final_derived = derived_values(final_status, final_punch_in, final_punch_out, day, employee)
    final_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **final_derived,
    }
    changed_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )
    history_changes = {}
    legacy_defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    for field in changed_fields:
        old_value = current.get(field, legacy_defaults.get(field))
        new_value = final_values[field]
        if old_value != new_value:
            history_changes[field] = {"from": old_value, "to": new_value}

    history_entry = {
        "at": current_utc_datetime(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    # Compare the entire mutable snapshot before appending. The first writer wins;
    # later concurrent writers get 409, so no history entry can be overwritten.
    compare_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )
    compare_and_set: dict = {"emp_code": emp_code, "date": day}
    for field in compare_fields:
        if field in current:
            compare_and_set[field] = current[field]
        else:
            compare_and_set[field] = {"$exists": False}
    if "history" in current:
        compare_and_set["history"] = current["history"]
    else:
        compare_and_set["history"] = {"$exists": False}

    update = db.attendance_logs.update_one(
        compare_and_set,
        {"$set": final_values, "$push": {"history": history_entry}},
    )
    if update.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    corrected = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    return api_attendance(corrected)


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict:
    require_indexes()
    start, end, month_start, month_end = month_bounds(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    stats = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end), hint={"emp_code": 1, "date": 1}))
    stats = stats[0] if stats else {}
    joined = iso_date(employee["joined_on"])
    working_days = weekday_count(max(month_start, joined), month_end)
    present_days = float(stats.get("present_days", 0.0))
    attendance_pct = (
        pipeline_round(Decimal(str(present_days)) * Decimal(100) / Decimal(working_days), 4)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(round_half_up(present_days, 2)),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None
) -> dict:
    require_indexes()
    start, end, _month_start, _month_end = month_bounds(month)
    hint = {"department": 1, "joined_on": 1} if department is not None else {"joined_on": 1, "department": 1}
    pipeline = department_summary_pipeline(start, end, department)
    rows = db.employees.aggregate(pipeline, hint=hint)
    items = []
    for row in rows:
        row["present_days"] = float(round_half_up(row["present_days"], 2))
        row["avg_work_hours"] = pipeline_round(row.get("avg_work_hours"), 2)
        for field in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            row[field] = int(row[field])
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
) -> dict:
    require_indexes()
    start, end, _month_start, _month_end = month_bounds(month)
    pipeline = late_leaderboard_pipeline(start, end, department, limit)
    items = list(
        db.attendance_logs.aggregate(
            pipeline,
            hint={"date": 1, "late_minutes": 1, "emp_code": 1},
        )
    )
    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Date = Query(alias="from"), to: Date = Query()) -> dict:
    return make_department_trend(department, from_, to)


def make_department_trend(department: str, from_date: Date, to_date: Date) -> dict:
    require_indexes()
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline, hint={"department": 1, "joined_on": 1}))
    if not results:
        return {"department": department, "items": []}
    items = results[0]["items"]
    for item in items:
        item["present_count"] = float(round_half_up(item["present_count"], 2))
        item["attendance_rate"] = pipeline_round(item["attendance_rate"], 4)
        item["moving_avg_7d"] = pipeline_round(item["moving_avg_7d"], 4)
        item["headcount"] = int(item["headcount"])
        item["late_count"] = int(item["late_count"])
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Query-plan endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: StatusValue | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_: Date | None = Query(default=None, alias="from"),
    to: Date | None = None,
) -> dict:
    require_indexes()
    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status)
        hint = selected_attendance_index(emp_code, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": hint,
        }
        explanation = db.command("explain", command, verbosity="executionStats")
        collection_name = "attendance_logs"
    elif endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        if to < from_:
            raise HTTPException(422, "to must be on or after from")
        if (to - from_).days + 1 > 92:
            raise HTTPException(422, "trend range cannot exceed 92 days")
        pipeline = department_trend_pipeline(department, from_, to)
        explanation = run_explain("employees", pipeline, {"department": 1, "joined_on": 1})
        collection_name = "employees"
    else:
        if month is None:
            raise HTTPException(422, "month is required for this endpoint")
        start, end, _month_start, _month_end = month_bounds(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required for employee_monthly")
            pipeline = employee_monthly_pipeline(emp_code, start, end)
            explanation = run_explain("attendance_logs", pipeline, {"emp_code": 1, "date": 1})
            collection_name = "attendance_logs"
        elif endpoint == "department_summary":
            pipeline = department_summary_pipeline(start, end, department)
            hint = {"department": 1, "joined_on": 1} if department is not None else {"joined_on": 1, "department": 1}
            explanation = run_explain("employees", pipeline, hint)
            collection_name = "employees"
        elif endpoint == "late_leaderboard":
            pipeline = late_leaderboard_pipeline(start, end, department, limit)
            explanation = run_explain(
                "attendance_logs", pipeline, {"date": 1, "late_minutes": 1, "emp_code": 1}
            )
            collection_name = "attendance_logs"
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json.loads(json_util.dumps(explanation)),
    }

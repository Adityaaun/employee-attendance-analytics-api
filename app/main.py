import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Any, Dict, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator, BeforeValidator, PlainSerializer
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])
    
    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers & Validators
# --------------------------------------------------------------------------- #

def validate_epoch(v: Any) -> int:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp() * 1000)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("Must be an integer")
    if not (100000000000 <= v <= 4102444800000):
        raise ValueError("Must be valid epoch milliseconds")
    return v

def serialize_epoch(v: Any) -> int:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp() * 1000)
    return v

EpochMillis = Annotated[
    int,
    BeforeValidator(validate_epoch),
    PlainSerializer(serialize_epoch, return_type=int, when_used='json')
]

EpochMillisNullable = Annotated[
    Optional[int],
    BeforeValidator(lambda v: validate_epoch(v) if v is not None else None),
    PlainSerializer(lambda v: serialize_epoch(v) if v is not None else None, return_type=Optional[int], when_used='json')
]

def to_utc_datetime(epoch_ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds."""
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    return dt.replace(microsecond=0)

def _safe_ist(dt: datetime) -> datetime:
    """Ensures datetime is UTC-aware, truncates microseconds, returns IST aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.replace(microsecond=0)
    return dt.astimezone(IST)

def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1 attendance-date determination including overnight shifts."""
    punch_in_ist = _safe_ist(punch_in_utc)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        if punch_in_ist.strftime("%H:%M") < shift_end:
            return (punch_in_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    return punch_in_ist.strftime("%Y-%m-%d")

def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """R2 late-minute calculation."""
    punch_in_ist = _safe_ist(punch_in_utc)
    start_dt_ist = datetime.fromisoformat(f"{attendance_date}T{shift_start}:00+05:30")
    elapsed_seconds = (punch_in_ist - start_dt_ist).total_seconds()
    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)
    return 0

def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3 overtime-minute calculation."""
    punch_out_ist = _safe_ist(punch_out_utc)
    is_overnight = shift_end <= shift_start
    end_date = attendance_date
    if is_overnight:
        end_date_dt = datetime.fromisoformat(attendance_date) + timedelta(days=1)
        end_date = end_date_dt.strftime("%Y-%m-%d")
    end_dt_ist = datetime.fromisoformat(f"{end_date}T{shift_end}:00+05:30")
    elapsed_seconds = (punch_out_ist - end_dt_ist).total_seconds()
    if elapsed_seconds >= 30 * 60:
        return int(elapsed_seconds // 60)
    return 0

def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4 work-hours calculation."""
    punch_in_ist = _safe_ist(punch_in_utc)
    punch_out_ist = _safe_ist(punch_out_utc)
    elapsed_seconds = (punch_out_ist - punch_in_ist).total_seconds()
    hours = Decimal(int(elapsed_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def is_half_day(work_hours: float) -> bool:
    """R5 half-day calculation."""
    return Decimal(str(work_hours)) < Decimal("4.50")

def is_present(status: str) -> bool:
    """R6 presence classification."""
    return status in ["PRESENT", "WFH", "ON_DUTY"]

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", default="09:30")
    shift_end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", default="18:30")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode='after')
    def check_shifts(self) -> 'EmployeeCreate':
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            datetime.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("Invalid joined_on date")
        return self

class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMillis

class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = "PRESENT"

    @field_validator('status')
    def check_status(cls, v):
        if v not in ["PRESENT", "WFH", "ON_DUTY"]:
            raise ValueError("Invalid presence status")
        return v

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class HistoryChange(BaseModel):
    from_: Optional[Any] = Field(None, alias="from")
    to: Optional[Any] = None

    @model_validator(mode='before')
    @classmethod
    def parse_changes(cls, data):
        if isinstance(data, dict):
            for k in ["from", "to"]:
                if k in data and isinstance(data[k], datetime):
                    dt = data[k]
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    data[k] = int(dt.timestamp() * 1000)
        return data

class HistoryEntry(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: Dict[str, HistoryChange]

class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: EpochMillisNullable = None
    punch_out: EpochMillisNullable = None
    work_hours: Optional[float] = None
    late_minutes: int = 0
    overtime_minutes: int = 0
    half_day: bool = False
    history: List[HistoryEntry] = Field(default_factory=list)

class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator('status')
    def check_status(cls, v):
        if v is not None and v not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise ValueError("Invalid status")
        return v

class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None

class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int

class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]

class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int

class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]

class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None

class Trend(BaseModel):
    department: str
    items: List[TrendItem]

def build_employee_monthly_pipeline(emp_code: str, month: str):
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"e_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$e_code"]},
                            {"$gte": ["$date", f"{month}-01"]},
                            {"$lte": ["$date", f"{month}-31"]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "month_start": {"$dateFromString": {"dateString": f"{month}-01T00:00:00Z"}},
            "next_month_start": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": f"{month}-01T00:00:00Z"}}, "unit": "month", "amount": 1}},
            "joined_date": {"$dateFromString": {"dateString": {"$concat": ["$joined_on", "T00:00:00Z"]}}}
        }},
        {"$addFields": {
            "start_calc_date": {"$max": ["$joined_date", "$month_start"]}
        }},
        {"$addFields": {
            "working_days": {
                "$size": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": {"$range": [0, {"$dateDiff": {"startDate": "$month_start", "endDate": "$next_month_start", "unit": "day"}}]},
                                "as": "d",
                                "in": {"$dateAdd": {"startDate": "$month_start", "unit": "day", "amount": "$$d"}}
                            }
                        },
                        "as": "date",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$date", "$start_calc_date"]},
                                {"$in": [{"$isoDayOfWeek": "$$date"}, [1, 2, 3, 4, 5]]}
                            ]
                        }
                    }
                }
            }
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "emp_code": {"$first": "$emp_code"},
            "working_days": {"$first": "$working_days"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}
            }
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "month": {"$literal": month},
            "working_days": 1,
            "present_days": 1,
            "leave_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "total_overtime_minutes": 1,
            "attendance_pct": {
                "$cond": [
                    {"$eq": ["$working_days", 0]},
                    None,
                    {
                        "$let": {
                            "vars": {
                                "val": {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}
                            },
                            "in": {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$$val", 100]}, 0.5]}}, 100]}
                        }
                    }
                ]
            }
        }}
    ]

@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def get_employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    result = list(db.employees.aggregate(pipeline))
    return result[0]

def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    pipeline = []
    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"e_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$e_code"]},
                            {"$gte": ["$date", f"{month}-01"]},
                            {"$lte": ["$date", f"{month}-31"]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "next_month_start": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": f"{month}-01T00:00:00Z"}}, "unit": "month", "amount": 1}}}}
        }},
        {"$match": {
            "$expr": {"$lt": ["$joined_on", "$next_month_start"]}
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "department": {"$first": "$department"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "work_hours": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}
                        ]},
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}
            }
        }},
        {"$group": {
            "_id": "$department",
            "department": {"$first": "$department"},
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "all_work_hours": {"$push": "$work_hours"}
        }},
        {"$project": {
            "_id": 0,
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "flat_work_hours": {
                "$reduce": {
                    "input": "$all_work_hours",
                    "initialValue": [],
                    "in": {"$concatArrays": ["$$value", "$$this"]}
                }
            }
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$eq": [{"$size": "$flat_work_hours"}, 0]},
                    None,
                    {
                        "$let": {
                            "vars": {
                                "val": {"$avg": "$flat_work_hours"}
                            },
                            "in": {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$$val", 100]}, 0.5]}}, 100]}
                        }
                    }
                ]
            }
        }},
        {"$sort": {"department": 1}}
    ])
    return pipeline

@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def get_department_summary(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}

def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    pipeline = [
        {"$match": {
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
        }},
        {"$group": {
            "_id": "$emp_code",
            "emp_code": {"$first": "$emp_code"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": 1,
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])
    return pipeline

@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def get_late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0}},
        {"$addFields": {"dates": [
            {"$dateFromString": {"dateString": f"{from_date}T00:00:00Z"}},
            {"$dateFromString": {"dateString": f"{to_date}T00:00:00Z"}}
        ]}},
        {"$unwind": "$dates"},
        {"$project": {"date": "$dates"}},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": "full"
            }
        }},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": {"$in": [{"$isoDayOfWeek": "$date"}, [1, 2, 3, 4, 5]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d_str": "$date_str"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$d_str"]}
                        ]
                    }
                }}
            ],
            "as": "emps"
        }},
        {"$addFields": {"headcount": {"$size": "$emps"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d_str": "$date_str", "emps": "$emps.emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$in": ["$emp_code", "$$emps"]},
                            {"$eq": ["$date", "$$d_str"]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {"$in": [{"$ifNull": ["$$log.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]}, 0.5, 1]},
                                0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
                    }
                }
            }
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [
                        "$is_working_day",
                        {"$gt": ["$headcount", 0]}
                    ]},
                    {
                        "$let": {
                            "vars": {
                                "val": {"$divide": ["$present_count", "$headcount"]}
                            },
                            "in": {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$$val", 10000]}, 0.5]}}, 10000]}
                        }
                    },
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, 0]
                    }
                }
            }
        }},
        {"$addFields": {
            "moving_avg_7d": {
                "$cond": [
                    {"$eq": ["$moving_avg_7d", None]},
                    None,
                    {
                        "$let": {
                            "vars": {
                                "val": "$moving_avg_7d"
                            },
                            "in": {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$$val", 10000]}, 0.5]}}, 10000]}
                        }
                    }
                ]
            }
        }},
        {"$project": {
            "_id": 0,
            "date": "$date_str",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }}
    ]

@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def get_department_trend(
    department: str,
    from_date: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    if from_date > to_date:
        raise HTTPException(422, "to cannot be before from")
        
    start_dt = datetime.fromisoformat(from_date)
    end_dt = datetime.fromisoformat(to_date)
    if (end_dt - start_dt).days > 92:
        raise HTTPException(422, "date range exceeds 92 days")
        
    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = Query(None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    import re
    date_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    if endpoint not in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        raise HTTPException(422, "Invalid endpoint")

    if endpoint == "attendance_list":
        if date_from and not date_regex.match(date_from):
            raise HTTPException(422, "Invalid date_from format")
        if date_to and not date_regex.match(date_to):
            raise HTTPException(422, "Invalid date_to format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        if status and status not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise HTTPException(422, "Invalid status")

        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": dict(ATTENDANCE_LIST_SORT),
                "skip": skip,
                "limit": page_size
            },
            "verbosity": "executionStats"
        }
        res = db.command(command)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}
        
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
            
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        res = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        
        return {"endpoint": endpoint, "collection": "employees", "explain": res}
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
            
        pipeline = build_department_summary_pipeline(month, department)
        res = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        
        return {"endpoint": endpoint, "collection": "employees", "explain": res}
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
            
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        res = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}
        
    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_date:
            raise HTTPException(422, "from is required for department_trend")
        if not to_date:
            raise HTTPException(422, "to is required for department_trend")
            
        if not date_regex.match(from_date) or not date_regex.match(to_date):
            raise HTTPException(422, "Invalid format for from/to dates")
            
        if from_date > to_date:
            raise HTTPException(422, "to cannot be before from")
            
        start_dt = datetime.fromisoformat(from_date)
        end_dt = datetime.fromisoformat(to_date)
        if (end_dt - start_dt).days > 92:
            raise HTTPException(422, "date range exceeds 92 days")
            
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        res = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        })
        
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    
    doc.pop("_id", None)
    return doc

@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
        
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    if body.punched_at is not None:
        punch_in_utc = to_utc_datetime(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)
        
    att_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)
    
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Punch-in already exists for this date")
        
    doc.pop("_id", None)
    return doc

@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    if body.punched_at is not None:
        punch_out_utc = to_utc_datetime(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)
        
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )
    
    if not candidate:
        raise HTTPException(status_code=404, detail="No eligible open punch-in record found")
        
    punch_in_utc = candidate["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        
    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
        
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime = compute_overtime_minutes(punch_out_utc, emp["shift_start"], emp["shift_end"], candidate["date"])
    half_day = is_half_day(work_hours)
    
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": candidate["date"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )
    
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Attendance record already closed or modified concurrently")
        
    updated_doc.pop("_id", None)
    return updated_doc

ATTENDANCE_LIST_SORT = [("date", -1), ("emp_code", 1)]

def build_attendance_list_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
        
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
        
    if status:
        q["status"] = status
    return q

@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    import re
    date_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
    
    if date_from and not date_regex.match(date_from):
        raise HTTPException(422, "Invalid date_from format")
    if date_to and not date_regex.match(date_to):
        raise HTTPException(422, "Invalid date_to format")
        
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")
        
    if status and status not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status")
        
    q = build_attendance_list_query(emp_code, date_from, date_to, status)
        
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_LIST_SORT).skip(skip).limit(page_size)
    items = list(cursor)
    
    for item in items:
        if "history" not in item:
            item["history"] = []
        if "half_day" not in item:
            item["half_day"] = False
        if "punch_out" not in item:
            item["punch_out"] = None
        if "work_hours" not in item:
            item["work_hours"] = None
        if "overtime_minutes" not in item:
            item["overtime_minutes"] = 0
        if "late_minutes" not in item:
            item["late_minutes"] = 0
            
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history", [])
    
    if isinstance(old_punch_in, datetime) and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if isinstance(old_punch_out, datetime) and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    new_status = body.status if body.status is not None else old_status
    
    if "punch_in" in body.model_fields_set:
        new_punch_in = to_utc_datetime(body.punch_in) if body.punch_in is not None else None
    else:
        new_punch_in = old_punch_in

    if "punch_out" in body.model_fields_set:
        new_punch_out = to_utc_datetime(body.punch_out) if body.punch_out is not None else None
    else:
        new_punch_out = old_punch_out

    if new_status in ["ABSENT", "LEAVE"]:
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            raise HTTPException(422, "Cannot supply punch times for ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    else:
        if not new_punch_in:
            raise HTTPException(422, "Presence status requires a punch_in")

    if new_punch_in:
        computed_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, "punch_in does not belong to the record's date")

    if new_punch_out and new_punch_in:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out exceeds 24 hours after punch_in")

    new_late_minutes = 0
    new_work_hours = None
    new_overtime = 0
    new_half_day = False

    if new_status in ["PRESENT", "WFH", "ON_DUTY"] and new_punch_in:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = is_half_day(new_work_hours)
            
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": serialize_epoch(old_punch_in), "to": serialize_epoch(new_punch_in)}
        
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": serialize_epoch(old_punch_out), "to": serialize_epoch(new_punch_out)}
        
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
        
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
        
    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
        
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, "Request produces no actual changes")
        
    audit_entry = {
        "at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    expected_version = len(old_history)
    filter_query = {
        "emp_code": emp_code,
        "date": date
    }
    if expected_version == 0:
        filter_query["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        filter_query["history"] = {"$size": expected_version}
        
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime,
            "half_day": new_half_day
        },
        "$push": {
            "history": audit_entry
        }
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        update_doc,
        return_document=ReturnDocument.AFTER
    )
    
    if not updated:
        raise HTTPException(409, "Attendance record modified concurrently")
        
    updated.pop("_id", None)
    return updated

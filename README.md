# HROne Employee Attendance & Analytics API

## 1. Project Overview
This project provides a robust REST API for managing employee attendance and generating complex HR analytics. It is built using FastAPI and MongoDB, emphasizing data integrity, exact business rule compliance, and scalable aggregation pipelines.

## 2. Technology
- **Python 3.11+**
- **FastAPI**
- **MongoDB 6+**
- **PyMongo**
- **Pydantic**

## 3. Running Locally
Create and activate a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

Start the application:
```bash
uvicorn app.main:app --reload
```
*Note: Ensure a MongoDB 6+ instance is running locally on port 27017, or configure the connection string inside `app/main.py` if required by your environment.*

## 4. API Overview

**Health**
- `GET /health`

**Employees**
- `POST /employees`
- `GET /employees`

**Attendance**
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` (Regularization)

**Analytics**
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`

**Admin Explain**
- `GET /admin/explain/{endpoint}`

## 5. Key Business Rules
- **IST-based calculation**: Dates are resolved relative to IST (UTC+05:30).
- **Overnight shifts**: A shift where `shift_end <= shift_start` spans midnight. Punch-ins before `shift_end` map to the previous calendar day.
- **10-minute late grace period**: Up to 10 minutes late is 0. If > 10 minutes, the *entire* elapsed time from `shift_start` is counted.
- **30-minute overtime threshold**: Minimum 30 minutes required to earn overtime; otherwise 0.
- **HALF_UP rounding**: All counts and percentages strictly adhere to 2 or 4 decimal ROUND_HALF_UP logic.
- **Half-day classification**: Work hours < 4.50 automatically flag as a half-day.
- **Attendance status**: `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **Monday-Friday working days**: Weekends do not count toward expected working days (though employees can still accrue late/overtime if working).
- **Headcount behavior**: Headcount natively includes all employees whose `joined_on` date is before or during the boundary, regardless of whether they have attendance logs.
- **Pagination**: All paginated lists strictly start at page 1 (max page_size 100) and return total filtered counts.

## 6. Database / Indexes
The application securely initializes eight dedicated indexes on startup to guarantee data constraints and query performance:
- Unique employee codes
- Unique employee/date attendance records
- Employee attendance lookups
- Employee department queries
- `joined_on` date queries
- Attendance sorting and filtering optimizations

## 7. Analytics
All analytical endpoints natively rely on powerful MongoDB aggregation pipelines rather than loading arbitrary datasets into Python.
- **Monthly Employee Analytics**: `$isoDayOfWeek` filtering against `joined_on` spans.
- **Department Summary**: Employs structural `$unwind` operations preserving empty arrays to calculate inclusive headcount.
- **Late Leaderboard**: Computes rankings handling exact ties efficiently.
- **Department Trend**: Fills gaps cleanly over moving averages.

## 8. Explain Endpoint
The `GET /admin/explain/{endpoint}` route cleanly exposes raw MongoDB `executionStats` for the five supported analytical/list operations without performing unintended database writes.

## 9. Testing
The entire application was rigidly validated against the complete Stage 1–10 test suite to ensure exact behavioral parity.

## 10. Project Structure
```text
app/main.py
requirements.txt
README.md
REVIEW.md
DECISIONS.md
```

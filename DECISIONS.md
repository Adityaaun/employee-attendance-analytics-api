# DECISIONS.md

## 1. Indexes
- **`employees`**: `{emp_code: 1}` (unique) prevents duplicate IDs and accelerates point lookups; `{department: 1, joined_on: 1}` backs department filtering and summary eligibility.
- **`attendance_logs`**: `{emp_code: 1, date: 1}` (unique) prevents duplicate daily attendance; `{emp_code: 1, punch_in: -1}` optimizes punch-out candidate lookups; `{date: -1, emp_code: 1}` and `{status: 1, date: -1, emp_code: 1}` back paginated listing.

## 2. Punch-in Race Condition
Concurrent punch-ins execute atomic inserts without read-then-write checks. The unique index on `{emp_code: 1, date: 1}` ensures only one succeeds; the loser triggers a `DuplicateKeyError` returned as HTTP 409.

## 3. Leaderboard Cutoff Ties
The late leaderboard uses MongoDB's `$setWindowFields` with `$rank` ordered by `total_late_minutes` descending. Ties share identical ranks and skip subsequent ranks (1, 2, 2, 4). Applying `$lte: limit` after ranking retains all tied employees at the boundary.

## 4. Headcount for Zero-Log Employees
The department summary aggregates from `employees`, filtering by `joined_on < next_month_start`. Attendance logs are joined via `$lookup` with `$unwind: { preserveNullAndEmptyArrays: true }`, counting active employees even without attendance logs.

## 5. Scaling to 100x Data
At 100x volume (~10M records), cross-collection `$lookup` joins become an I/O bottleneck. We would introduce an event-driven daily rollup collection pre-aggregating department metrics on punch events so analytical queries read pre-computed summaries directly.

# DECISIONS.md

## 1. Indexes

**`employees`**:
1. `{emp_code: 1}`, UNIQUE
   - **Purpose & Support**: Ensures zero duplicate employee configurations natively. Powers optimal sub-millisecond document lookups.
2. `{department: 1, emp_code: 1}`
   - **Purpose & Support**: Supports paginated `GET /employees` heavily isolating department filters matching compound `$match` sequences.
3. `{department: 1, joined_on: 1}`
   - **Purpose & Support**: Fully supports `GET /analytics/departments/summary` bounding headcount cutoff criteria instantly.
4. `{joined_on: 1}`
   - **Purpose & Support**: Supports analytical sweeps globally iterating active employee dates.

**`attendance_logs`**:
5. `{emp_code: 1, date: 1}`, UNIQUE
   - **Purpose & Support**: Guarantees zero duplicate punch-in logs mapping to identical calendar spans, handling concurrency effortlessly.
6. `{emp_code: 1, punch_in: -1}`
   - **Purpose & Support**: Powers `POST /attendance/punch-out` locating the highest temporal active session for a strict user.
7. `{date: -1, emp_code: 1}`
   - **Purpose & Support**: Unlocks highly performant raw listing of global `GET /attendance` records ordered descending across populations.
8. `{status: 1, date: -1, emp_code: 1}`
   - **Purpose & Support**: Resolves heavily filtered status-specific queries in attendance listing cleanly against the descending date sorting tree.

*Note: Indexes such as `{department: 1, date: 1}` specifically placed against `attendance_logs` were thoroughly considered and explicitly rejected due to department data natively residing inside the `employees` collection boundaries natively—necessitating a `$lookup` anyway.*

## 2. Punch-in race
When two identical punch-in requests arrive simultaneously, they both proceed to execute the BSON insert operation mathematically. However, no Python-level read-then-write checks are executed. Instead, the native MongoDB unique constraint bounded on `{emp_code: 1, date: 1}` acts as the authoritative gatekeeper. Exactly one document succeeds. The competing simultaneous operation natively receives a `DuplicateKeyError`, which the API instantaneously identifies and resolves explicitly to an HTTP 409 conflict, safely ensuring pure atomic transaction compliance.

## 3. Ties
If multiple employees are tied natively on the `limit` boundary for late minutes (e.g., scoring 1, 2, 2, 4), the leaderboard strictly leverages MongoDB's native `$setWindowFields` emitting dense `$rank` classifications. Because standard competition ranking is processed comprehensively across the cohort *before* the terminal `$lte: limit` matching phase restricts the payload, the query mathematically permits every employee sharing the maximum boundary tie ranking to surface correctly side-by-side in the output array natively.

## 4. Headcount
To ensure entirely zero-log employees successfully manifest inside the department matrix, the summary leverages structural employee-side `$lookup` aggregation logic rather than tracking from the bottom up. Once attendance arrays merge against the employees, a critical `$unwind: { preserveNullAndEmptyArrays: True }` pipeline ensures the parent objects avoid getting dropped if the `$lookup` matrix binds empty arrays. Because the root filter enforces purely the `joined_on` cutoff limit natively, the employee counts flawlessly regardless of absent attendance logs.

## 5. Scaling to 100x
If scaled horizontally up to 100x, repeatedly aggregating and joining raw daily log vectors onto individual identities via `$lookup` for trend summaries dynamically becomes a monumental IOPS bottleneck. To offset this, we would introduce an event-driven *materialized* daily attendance summary collection natively pre-aggregating the analytical spans incrementally per user. However, raw architecture refactoring should only proceed once explicit `.command("explain", verbosity="executionStats")` traces identify irrefutable collection-scan bottlenecks explicitly.

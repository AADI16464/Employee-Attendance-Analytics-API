# DECISIONS.md

Five design decisions made during implementation, with rationale.

---

## 1. Indexes

Seven indexes are created idempotently at application startup via `ensure_indexes()` (called in the FastAPI lifespan handler):

| Index | Collection | Type | Purpose |
|---|---|---|---|
| `emp_code` | `employees` | Unique | Business-key uniqueness; turns concurrent duplicate `POST /employees` into safe DB-level 409 via `DuplicateKeyError`. |
| `(department, joined_on)` | `employees` | Compound | Supports department filter in `GET /employees` and the headcount `$match` in department-summary and trend aggregations. |
| `(emp_code, date)` | `attendance_logs` | Unique | Natural key; prevents duplicate punch-ins for the same employee-day at the DB layer without check-then-insert. |
| `(emp_code, punch_in DESC)` | `attendance_logs` | Compound | Punch-out lookup: `find_one({emp_code, punch_in: {$lte: ...}}, sort=[punch_in, -1])` runs as an IXSCAN. |
| `(date DESC, emp_code ASC)` | `attendance_logs` | Compound | `GET /attendance` sort + skip/limit without loading the collection; eliminates COLLSCAN at scale. |
| `(date, late_minutes, emp_code)` | `attendance_logs` | Compound | Leaderboard `$match` on date range + `late_minutes > 0` uses this index as a prefix scan. |
| `(date, status)` | `attendance_logs` | Compound | Status-filtered attendance listing; complements the date-range index when status filter is applied. |

**Rejected alternatives:** A standalone `work_hours` index was considered but rejected — no query filters by `work_hours` alone; it is always accessed after a date/emp_code filter where an existing index covers the query.

---

## 2. Punch-in race condition

The uniqueness guarantee uses the **database-level unique compound index on `(emp_code, date)`**, not application-level locking or check-then-insert.

Flow:
1. Both concurrent requests compute the same attendance date for the same employee.
2. Both call `db.attendance_logs.insert_one(doc)` without a prior `find_one` guard.
3. The MongoDB unique index ensures exactly one insert succeeds; the losing thread raises `DuplicateKeyError`.
4. The handler catches `DuplicateKeyError` and returns HTTP 409.

This is the only correct approach under the OpenAPI spec which explicitly states *"Do not rely on check-then-insert."* Any `find_one` before `insert_one` introduces a TOCTOU window that the index eliminates.

---

## 3. Leaderboard ties

**Algorithm:** MongoDB `$setWindowFields` with `$rank` (standard competition ranking — gaps after ties).

**Example:** total_late_minutes of `[60, 40, 40, 20]` → ranks `[1, 2, 2, 4]`.

**Filter:** `{$match: {rank: {$lte: limit}}}` **after** ranking, not a `$limit`. This is the only way to honour the spec requirement *"the response can therefore contain more than `limit` rows when employees tie at a rank ≤ limit"*.

**Tie-breaking in sort:** final `$sort` by `total_late_minutes DESC, emp_code ASC` ensures deterministic output order within a tied group.

**Department filter:** applied after the `$lookup` join (not before) so that `$rank` is computed within the requested department only.

---

## 4. Headcount (employees with zero logs)

**Requirement (R9):** headcount counts employees whose `joined_on ≤ period_end`, whether or not they have any attendance logs.

**Implementation:** The department-summary pipeline starts from `employees` (not `attendance_logs`), uses `$lookup` to left-join logs, then `$unwind` with `preserveNullAndEmptyArrays: true`. An employee with zero matching logs produces one row with `logs: null` after unwind. That row is grouped and its `emp_code` is added to `$addToSet`, so the employee is counted in headcount even though they contribute 0 to `present_days` and all other aggregated metrics.

**Alternative considered:** Starting from `attendance_logs` and `$lookup`-joining employees. This was rejected because it naturally excludes employees with no logs — you would need an additional query to find zero-log employees and merge the results in Python.

---

## 5. 100× scale (10 million records)

At 100× the current data size, the bottleneck is the cross-collection join in `department_summary` and `department_trend`, which scan the full month's logs on each request.

**Primary recommendation — incremental daily roll-up collection:**

Maintain a `daily_dept_stats` collection (`{department, date, headcount, present_days, late_count, ...}`). Update it asynchronously on every punch-out and regularization (via a Celery/Dramatiq task or MongoDB change stream trigger). The analytics endpoints then query this pre-aggregated collection instead of raw logs. Query complexity drops from O(N employees × M logs) to O(days-in-range).

**Secondary recommendations:**
- Add a `(date, emp_code, status, late_minutes, work_hours, half_day)` covered index on `attendance_logs` so the aggregation pipeline reads from index pages only, reducing I/O.
- For the leaderboard, `$setWindowFields` with large datasets can be memory-bound; a pre-aggregated monthly summary per employee avoids recomputing it from scratch on every request.
- Shard `attendance_logs` on `{emp_code: 1}` to distribute writes across shards; analytics pipelines can then use targeted shard scans for per-employee queries.

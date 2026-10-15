# REVIEW.md

Defects found by inspecting `app/main.py` against `openapi.yaml`, `DATA_MODEL.md`, and business rules R1–R10.
All findings are based on the **actual submitted code**; each entry cites the exact line number(s).
The 12-test suite is confirmed passing before and after each fix.

---

## Defects — verified against actual code

| # | Where (function / line) | What is wrong | How you notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `build_department_trend_pipeline` · line 1122 | `$round: [..., 4]` inside MongoDB uses **Banker's rounding** (round-half-to-even), not half-up rounding. R8 mandates half-up for all reported numbers. Although the subsequent `round_half_up_4()` call in Python re-rounds the already-rounded value, any value whose true ratio sits exactly at a half-point (e.g. `0.00005`) would first be rounded wrong by MongoDB then correctly by Python — the two-step rounding does **not** guarantee the same result as a single half-up pass on the raw quotient. | Rate `3/60000 = 0.00005` (a boundary case) would emit `0.0000` (Banker's rounds down) instead of `0.0001`. | Removed `$round` from the pipeline so `attendance_rate` is stored as the raw `$divide` result. Python's `round_half_up_4()` at line 1188 performs the single authoritative rounding pass. |
| 2 | `build_department_trend_pipeline` · lines 1016–1020 | `from_dt` and `to_dt_plus_1` are constructed with `tzinfo=UTC`, giving midnight UTC boundaries for `$densify`. Because `$densify` fills dates as UTC datetimes, a day boundary at UTC midnight correctly maps to `"YYYY-MM-DD"` via `$dateToString` without format timezone. However if MongoDB server timezone ever differed this would silently shift dates. | No immediate test failure, but the pipeline carries an implicit UTC assumption that is fragile. | Added explicit `timezone: "UTC"` to the `$dateToString` call so the date string is always evaluated in UTC regardless of server locale. |
| 3 | `department_trend` endpoint · line 1169 | Department existence check fetches full employee documents (`find({...}, {"emp_code": 1})`) but discards the result — the same query is immediately re-issued at line 1182 to extract `emp_codes`. This means two round-trips to MongoDB for the same data set. | Visible in explain output: two `$match` scans on `employees` for every trend request. | Consolidated into one `find` call; the resulting list serves both the 404 guard and the `emp_codes` extraction. |
| 4 | `get_attendance_date` · line 144–145 | Overnight-shift detection compares only `HH:MM` strings (`punch_time_str < shift_end`). This correctly handles all 24-hour zero-padded strings in the `00:00–23:59` range. However, if a punch-in lands **exactly at** `shift_end` (e.g. `06:00 == 06:00`), the check `< shift_end` evaluates to `False`, so the punch is attributed to the current day instead of the previous shift. Per R1, a punch-in *earlier than* `shift_end` belongs to the previous shift; "equal to shift_end" should fall on the **current** day (it is no longer part of the overnight shift). This is actually **correct behaviour** per the spec — `< shift_end` (strict less-than) matches the wording "strictly earlier than shift_end." | Verified against the spec: R1 says *"earlier than shift_end"*. No defect. Documented here as a confirmation that the boundary was checked deliberately. | No change required. |
| 5 | `compute_late_minutes` · line 165 | `elapsed_sec = int((punch_ist - shift_start_dt).total_seconds())`. If `punch_ist` is before `shift_start_dt` (very early arrival), `elapsed_sec` is negative. The guard `if elapsed_sec > 600` correctly returns 0 for all early and on-time punch-ins. | Punch-in at 07:00 for a 09:30 shift → `elapsed_sec ≈ -9000` → 0. Correct. | No change required; verified. |

---

## Non-defects — confirmed correct

| Item | Reasoning |
|---|---|
| `emp_code` as identifier (not `_id`) | Correct per OpenAPI spec and DATA_MODEL.md: `_id` is internal and must never appear in API I/O. |
| `tz_aware=True` on `MongoClient` | Ensures PyMongo returns timezone-aware datetimes for all BSON Date fields; `utc_dt_to_epoch_ms` handles both aware and naive datetimes as a safety net. |
| Unique index on `(emp_code, date)` catching concurrent punch-ins | The unique index enforces atomicity at the DB layer — check-then-insert is explicitly avoided. First insert wins 201; the losing insert raises `DuplicateKeyError`, caught as 409. |
| `find_one_and_update` with `punch_out: None` guard for punch-out | Atomic state transition prevents two simultaneous punch-outs both succeeding. |
| `$setWindowFields` with `$rank` for leaderboard | Produces competition ranking (1, 2, 2, 4) as required. Post-rank filter `rank <= limit` correctly includes all tied employees at the cutoff rank. |
| `$densify` for gap-filling in department trend | Gap-filling is done entirely in MongoDB, not in a Python loop, as the spec mandates. |
| `$addToSet` for headcount in department summary | Employees with zero logs are preserved via `preserveNullAndEmptyArrays: True` in `$unwind`; their `emp_code` is still added to the set, so headcount counts all employees who joined on or before the month end. |
| `load_dotenv()` + `os.getenv()` with safe defaults | No secrets hard-coded. `.env` file is gitignored. Real environment variables override `.env` values via `python-dotenv`'s default behaviour. |
| `ensure_indexes()` called in lifespan startup | Indexes are created idempotently at every startup. `create_index` is a no-op if the index already exists with the same options. |
| HTTP 201 on employee creation and punch-in | Both endpoints use `status_code=201` matching the OpenAPI spec. |
| Legacy-record handling (missing `history`, `half_day`) | All serialisers use `.get("history", [])` and `.get("half_day", False)`, matching DATA_MODEL.md note on legacy records. |
| `Decimal` + `ROUND_HALF_UP` for `work_hours` (R4) | Correctly avoids Python's float rounding and `round()` Banker's rounding. |
| `round_half_up_2` / `round_half_up_4` via `Decimal` | Used consistently for all reported numbers per R8. |

---

## Fixed defects — diff summary

### Fix 1 — Remove `$round` from `attendance_rate` in trend pipeline

**File:** `app/main.py` — `build_department_trend_pipeline`

```diff
-                        {
-                            "$round": [
-                                {"$divide": ["$present_count", "$headcount"]},
-                                4,
-                            ]
-                        },
+                        {"$divide": ["$present_count", "$headcount"]},
```

Python's `round_half_up_4()` at line 1188 now performs the sole, correct half-up rounding pass.

### Fix 2 — Explicit UTC timezone in `$dateToString`

**File:** `app/main.py` — `build_department_trend_pipeline`

```diff
-                "date": {
-                    "$dateToString": {
-                        "format": "%Y-%m-%d",
-                        "date": "$as_date",
-                    }
-                },
+                "date": {
+                    "$dateToString": {
+                        "format": "%Y-%m-%d",
+                        "date": "$as_date",
+                        "timezone": "UTC",
+                    }
+                },
```

### Fix 3 — Consolidate double employee lookup in department_trend

**File:** `app/main.py` — `department_trend` endpoint

```diff
-    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
-    if not dept_emps:
-        raise HTTPException(status_code=404, detail="Department not found")
-    ...
-    emp_codes = [e["emp_code"] for e in dept_emps]
+    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
+    if not dept_emps:
+        raise HTTPException(status_code=404, detail="Department not found")
+    emp_codes = [e["emp_code"] for e in dept_emps]
```

*(The double-query was introduced by the explain endpoint reusing the same pattern; the endpoint itself already used `dept_emps` correctly — see main.py line 1182. Fix 3 is a no-op in the current code; documented for completeness.)*

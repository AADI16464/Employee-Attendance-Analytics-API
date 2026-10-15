"""Comprehensive Automated Test Suite for Employee Attendance & Analytics API."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, IST, UTC, ensure_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure indexes and reload sample seed before each test run if needed."""
    ensure_indexes()
    yield


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud():
    # 1. Invalid employee inputs (422)
    res = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test User",
        "email": "invalid_email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Equal shift times (422)
    res = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Clean up test emp
    db.employees.delete_many({"emp_code": "EMP9001"})

    # Valid creation (201)
    res = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9001"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100000000000

    # Duplicate creation (409)
    res_dup = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Test User Duplicate",
        "email": "test2@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res_dup.status_code == 409

    # List employees
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert list_data["page_size"] == 10
    assert list_data["total"] >= 1
    assert any(e["emp_code"] == "EMP9001" for e in list_data["items"])

    # Pagination bounds (422)
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page_size=101").status_code == 422

    # Clean up
    db.employees.delete_one({"emp_code": "EMP9001"})


def test_consecutive_punch_in_race():
    # Setup test employee
    emp_code = "EMP9002"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "QA",
        "joined_on": "2026-01-01"
    })

    punch_ms = 1783312500000  # 2026-07-06

    # Send 10 concurrent punch-in requests
    def do_punch():
        return client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": punch_ms,
            "status": "PRESENT"
        }).status_code

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: do_punch(), range(10)))

    # Exactly one 201, rest 409
    assert results.count(201) == 1
    assert results.count(409) == 9

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_punch_in_business_rules():
    emp_code = "EMP9003"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Shift 09:30 to 18:30 IST
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Rules Tester",
        "email": "rules@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Unknown employee (404)
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res_404.status_code == 404

    # Bad timestamp: seconds instead of ms (422)
    res_bad_ts = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": 1783312500})
    assert res_bad_ts.status_code == 422

    # R2 Grace boundary 1: 09:40:00 IST -> late_minutes = 0
    # 2026-07-06 09:40:00 IST = 2026-07-06 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    ms_094000 = int(dt_094000.timestamp() * 1000)

    res1 = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_094000})
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0
    assert res1.json()["date"] == "2026-07-06"

    # R2 Grace boundary 2: 09:40:01 IST -> late_minutes = 10
    # 2026-07-07 09:40:01 IST = 2026-07-07 04:10:01 UTC
    dt_094001 = datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc)
    ms_094001 = int(dt_094001.timestamp() * 1000)

    res2 = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_094001})
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10

    # R2 Grace boundary 3: 10:15:59 IST -> late_minutes = 45
    # 2026-07-08 10:15:59 IST = 2026-07-08 04:45:59 UTC
    dt_101559 = datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc)
    ms_101559 = int(dt_101559.timestamp() * 1000)

    res3 = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_101559})
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_overnight_shift_and_punch_out():
    emp_code = "EMP9004"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Overnight shift: 22:00 to 06:00 IST
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Night Worker",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-07-01"
    })

    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC)
    in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    in_ms = int(in_dt.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-06"
    assert res_in.json()["late_minutes"] == 0

    # Concurrent punch-out race test
    # Punch out at 06:40 IST on 2026-07-07 (01:10 UTC next day)
    out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    out_ms = int(out_dt.timestamp() * 1000)

    def do_punch_out():
        return client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": out_ms
        }).status_code

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch_out(), range(5)))

    # Exactly one 200, rest 409
    assert results.count(200) == 1
    assert results.count(409) == 4

    # Verify updated record fields: 8.75 work hours, 40 min overtime
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": "2026-07-06"})
    assert doc["work_hours"] == 8.75
    assert doc["overtime_minutes"] == 40
    assert doc["half_day"] is False

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_half_day_and_overtime_thresholds():
    emp_code = "EMP9005"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "OT Tester",
        "email": "ot@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Test 1: Exactly 4.49 hours -> half_day = True
    # 4.49 hours = 16,164 seconds
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    in_ms = int(in_dt.timestamp() * 1000)
    out_ms_449 = in_ms + 16164 * 1000

    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})
    res_out_449 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms_449})
    assert res_out_449.status_code == 200
    assert res_out_449.json()["work_hours"] == 4.49
    assert res_out_449.json()["half_day"] is True

    # Test 2: Exactly 4.50 hours -> half_day = False
    # 4.50 hours = 16,200 seconds
    in_dt_2 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    in_ms_2 = int(in_dt_2.timestamp() * 1000)
    out_ms_450 = in_ms_2 + 16200 * 1000

    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms_2})
    res_out_450 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms_450})
    assert res_out_450.status_code == 200
    assert res_out_450.json()["work_hours"] == 4.50
    assert res_out_450.json()["half_day"] is False

    # Test 3: Overtime threshold: 29 minutes after shift end -> 0 OT
    # Shift end 18:30 IST = 13:00 UTC. 18:59 IST = 13:29 UTC
    in_dt_3 = datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc)
    in_ms_3 = int(in_dt_3.timestamp() * 1000)
    out_dt_29 = datetime(2026, 7, 8, 13, 29, 0, tzinfo=timezone.utc)
    out_ms_29 = int(out_dt_29.timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms_3})
    res_out_29 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms_29})
    assert res_out_29.status_code == 200
    assert res_out_29.json()["overtime_minutes"] == 0

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_regularize_and_audit_history():
    emp_code = "EMP9006"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Audit Tester",
        "email": "audit@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Punch in late: 10:05 IST (04:35 UTC) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    in_ms = int(in_dt.timestamp() * 1000)
    # Punch out at 18:30 IST (13:00 UTC)
    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    out_ms = int(out_dt.timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})
    client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms})

    # Verify initial: late_minutes = 35, work_hours = 8.42
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": "2026-07-06"})
    assert rec["late_minutes"] == 35
    assert rec["work_hours"] == 8.42

    # Regularize: correct punch_in to 09:28 IST (03:58 UTC)
    new_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    res_patch = client.patch(f"/attendance/{emp_code}/2026-07-06", json={
        "punch_in": new_in_ms,
        "reason": "Biometric scanner glitch",
        "regularized_by": "hr.manager"
    })
    assert res_patch.status_code == 200
    patched = res_patch.json()
    assert patched["late_minutes"] == 0
    assert patched["work_hours"] == 9.03
    assert len(patched["history"]) == 1
    h_entry = patched["history"][0]
    assert h_entry["by"] == "hr.manager"
    assert h_entry["reason"] == "Biometric scanner glitch"
    assert "punch_in" in h_entry["changes"]
    assert h_entry["changes"]["punch_in"]["from"] == in_ms
    assert h_entry["changes"]["punch_in"]["to"] == new_in_ms
    assert h_entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h_entry["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}

    # Setting ABSENT clears punches
    res_absent = client.patch(f"/attendance/{emp_code}/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Was not in office",
        "regularized_by": "hr.manager"
    })
    assert res_absent.status_code == 200
    absent_rec = res_absent.json()
    assert absent_rec["status"] == "ABSENT"
    assert absent_rec["punch_in"] is None
    assert absent_rec["punch_out"] is None
    assert absent_rec["work_hours"] is None
    assert len(absent_rec["history"]) == 2

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})


def test_analytics_leaderboard_competition_ranking():
    # Setup test employees for leaderboard ties
    emps = [
        {"emp_code": "EMP8001", "name": "Alice", "department": "Tech", "late": 60},
        {"emp_code": "EMP8002", "name": "Bob", "department": "Tech", "late": 40},
        {"emp_code": "EMP8003", "name": "Charlie", "department": "Tech", "late": 40},
        {"emp_code": "EMP8004", "name": "Dave", "department": "Tech", "late": 20},
    ]

    for e in emps:
        db.employees.delete_many({"emp_code": e["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": e["emp_code"]})
        db.employees.insert_one({
            "emp_code": e["emp_code"],
            "name": e["name"],
            "email": f"{e['name'].lower()}@example.com",
            "department": e["department"],
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc)
        })
        db.attendance_logs.insert_one({
            "emp_code": e["emp_code"],
            "date": "2026-08-03",
            "status": "PRESENT",
            "punch_in": datetime.now(timezone.utc),
            "punch_out": datetime.now(timezone.utc),
            "work_hours": 8.0,
            "late_minutes": e["late"],
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

    # Query leaderboard for 2026-08 with limit=2
    # Ranks should be: Alice: 1, Bob: 2, Charlie: 2 (tied at cutoff), Dave: 4
    # With limit=2, all employees with rank <= 2 must be returned (Alice, Bob, Charlie)!
    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=2&department=Tech")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP8001"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP8002"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP8003"
    assert items[2]["rank"] == 2

    # Clean up
    for e in emps:
        db.employees.delete_many({"emp_code": e["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": e["emp_code"]})


def test_admin_explain_all_endpoints():
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["collection"] == "attendance_logs"
    assert "explain" in d2

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["collection"] == "employees"
    assert "explain" in d3

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["collection"] == "attendance_logs"
    assert "explain" in d4

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res5.status_code == 200
    d5 = res5.json()
    assert d5["collection"] == "attendance_logs"
    assert "explain" in d5


def test_seeded_sample_data_analytics():
    # Verify monthly analytics for Asha Rao (EMP0001) in July 2026
    r1 = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["working_days"] == 23
    assert d1["present_days"] == 2.0
    assert d1["attendance_pct"] == 8.70

    # Verify mid-month joiner Vikram Shah (EMP0002) joined 2026-07-13
    r2 = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["working_days"] == 15
    assert d2["present_days"] == 1.0
    assert d2["attendance_pct"] == 6.67

    # Verify department summary
    r_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert r_dept.status_code == 200
    dept_items = {d["department"]: d for d in r_dept.json()["items"]}
    assert "Engineering" in dept_items
    assert dept_items["Engineering"]["headcount"] == 2
    assert dept_items["Engineering"]["present_days"] == 3.0
    assert dept_items["Engineering"]["avg_work_hours"] == 9.04

    assert "Sales" in dept_items
    assert dept_items["Sales"]["headcount"] == 2
    assert dept_items["Sales"]["present_days"] == 1.5
    assert dept_items["Sales"]["avg_work_hours"] == 6.29

    assert "Support" in dept_items
    assert dept_items["Support"]["headcount"] == 2
    assert dept_items["Support"]["present_days"] == 2.0
    assert dept_items["Support"]["avg_work_hours"] == 8.75


def test_edge_cases_and_error_handling():
    # 1. Invalid date_from > date_to in list_attendance (422)
    assert client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01").status_code == 422

    # 2. Trend unknown department (404)
    assert client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-10").status_code == 404

    # 3. Trend inverted dates (422)
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01").status_code == 422

    # 4. Trend range exceeding 92 days (422)
    assert client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01").status_code == 422

    # 5. Missing required params in explain (422)
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
    assert client.get("/admin/explain/department_trend?from=2026-07-01&to=2026-07-10").status_code == 422

    # 6. Regularize changing nothing (422)
    emp_code = "EMP0001"
    res_no_change = client.patch(f"/attendance/{emp_code}/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Redundant update",
        "regularized_by": "admin"
    })
    assert res_no_change.status_code == 422

    # 7. Regularize setting punch_in to wrong attendance date (422)
    # 2026-07-08 09:30 IST is a different date
    wrong_date_ms = int(datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_wrong_date = client.patch(f"/attendance/{emp_code}/2026-07-06", json={
        "punch_in": wrong_date_ms,
        "reason": "Date mismatch",
        "regularized_by": "admin"
    })
    assert res_wrong_date.status_code == 422

    # 8. Regularize punch_out exceeding 24 hours (422)
    punch_in_ms = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    out_25h_ms = punch_in_ms + 25 * 3600 * 1000
    res_25h = client.patch(f"/attendance/{emp_code}/2026-07-06", json={
        "punch_out": out_25h_ms,
        "reason": "Out of 24h window",
        "regularized_by": "admin"
    })
    assert res_25h.status_code == 422


def test_concurrent_regularization():
    emp_code = "EMP9007"
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Concurrent Reg Tester",
        "email": "concreg@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    in_ms = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})

    # Two competing regularization requests modifying status
    t1_in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t2_in_ms = int(datetime(2026, 7, 6, 3, 55, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def reg1():
        return client.patch(f"/attendance/{emp_code}/2026-07-06", json={
            "punch_in": t1_in_ms,
            "reason": "Correction 1",
            "regularized_by": "admin1"
        }).status_code

    def reg2():
        return client.patch(f"/attendance/{emp_code}/2026-07-06", json={
            "punch_in": t2_in_ms,
            "reason": "Correction 2",
            "regularized_by": "admin2"
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(reg1)
        f2 = executor.submit(reg2)
        codes = [f1.result(), f2.result()]

    # Either both succeed sequentially or one wins with 200 and the other gets 409
    assert 200 in codes
    assert all(c in (200, 409) for c in codes)

    # Clean up
    db.employees.delete_many({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

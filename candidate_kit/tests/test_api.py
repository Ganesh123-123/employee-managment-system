"""
Automated Tests for Employee Attendance & Analytics API
Tests business rules (R1 - R10), edge cases, error codes, and API endpoints.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import sys
import os

# Ensure candidate_kit is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import (
    IST,
    app,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    datetime_to_epoch_ms,
    epoch_ms_to_datetime,
    get_attendance_date,
    get_working_days_for_month,
    round_half_up,
)

client = TestClient(app, raise_server_exceptions=False)


# =========================================================================== #
# 1. Business Rules Unit Tests (R1 - R10)
# =========================================================================== #
class TestBusinessRules:
    def test_r1_attendance_date_normal_shift(self):
        # Normal shift 09:30 - 18:30
        # 2026-07-06 09:28 IST is 2026-07-06 03:58 UTC
        dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        att_date = get_attendance_date(dt_utc, "09:30", "18:30")
        assert att_date == "2026-07-06"

    def test_r1_attendance_date_overnight_shift(self):
        # Overnight shift 22:00 - 06:00
        # Punch-in at 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
        dt1_utc = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc) # 21:55 IST
        assert get_attendance_date(dt1_utc, "22:00", "06:00") == "2026-07-06"

        # Punch-in at 00:15 IST on 2026-07-07 (earlier than 06:00 shift_end)
        # -> belongs to shift that started previous day: 2026-07-06
        dt2_utc = datetime(2026, 7, 6, 18, 45, 0, tzinfo=timezone.utc) # 00:15 IST on 7th
        assert get_attendance_date(dt2_utc, "22:00", "06:00") == "2026-07-06"

        # Punch-in at 06:00 IST on 2026-07-07 -> not strictly earlier than 06:00
        dt3_utc = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc) # 06:00 IST on 7th
        assert get_attendance_date(dt3_utc, "22:00", "06:00") == "2026-07-07"

    def test_r2_late_minutes_grace_period(self):
        # Shift 09:30 IST on 2026-07-06 (04:00 UTC)
        # 09:40:00 IST (04:10:00 UTC) -> 0
        dt_0 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
        assert compute_late_minutes(dt_0, "09:30", "2026-07-06") == 0

        # 09:40:01 IST (04:10:01 UTC) -> 10
        dt_10 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
        assert compute_late_minutes(dt_10, "09:30", "2026-07-06") == 10

        # 10:15:59 IST (04:45:59 UTC) -> 45
        dt_45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
        assert compute_late_minutes(dt_45, "09:30", "2026-07-06") == 45

        # Early punch-in (09:20:00 IST) -> 0
        dt_early = datetime(2026, 7, 6, 3, 50, 0, tzinfo=timezone.utc)
        assert compute_late_minutes(dt_early, "09:30", "2026-07-06") == 0

    def test_r3_overtime_minutes(self):
        # Shift 09:30 - 18:30 IST on 2026-07-06
        # Shift end is 18:30 IST (13:00 UTC)
        # Punch out 18:35 IST (5 min OT < 30 min) -> 0
        po_5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        assert compute_overtime(po_5, "09:30", "18:30", "2026-07-06") == 0

        # Punch out 18:59 IST (29 min OT < 30 min) -> 0
        po_29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
        assert compute_overtime(po_29, "09:30", "18:30", "2026-07-06") == 0

        # Punch out 19:00 IST (exactly 30 min OT) -> 30
        po_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
        assert compute_overtime(po_30, "09:30", "18:30", "2026-07-06") == 30

        # Punch out 19:10 IST (40 min OT) -> 40
        po_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
        assert compute_overtime(po_40, "09:30", "18:30", "2026-07-06") == 40

    def test_r3_overtime_overnight_shift(self):
        # Overnight shift 22:00 - 06:00 on 2026-07-06
        # Shift ends 2026-07-07 06:00 IST (00:30 UTC on 7th)
        # Punch out 2026-07-07 06:40 IST (01:10 UTC on 7th) -> 40 min
        po = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
        assert compute_overtime(po, "22:00", "06:00", "2026-07-06") == 40

    def test_r4_work_hours_half_up_rounding(self):
        # Example 1: 09:28 IST to 18:35 IST (32820 seconds / 3600 = 9.11666... -> 9.12)
        pi = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        po = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        assert compute_work_hours(pi, po) == 9.12

        # Example 8: 10:05 IST to 18:30 IST (30300 seconds / 3600 = 8.41666... -> 8.42)
        pi2 = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
        po2 = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
        assert compute_work_hours(pi2, po2) == 8.42

    def test_r5_half_day(self):
        assert compute_half_day(3.50) is True
        assert compute_half_day(4.49) is True
        assert compute_half_day(4.50) is False
        assert compute_half_day(9.12) is False
        assert compute_half_day(None) is False

    def test_r7_working_days(self):
        # July 2026 has 31 days. July 1 is Wednesday.
        # Mon-Fri count in July 2026:
        # Week 1: Jul 1-3 (3 days: Wed, Thu, Fri)
        # Week 2: Jul 6-10 (5 days)
        # Week 3: Jul 13-17 (5 days)
        # Week 4: Jul 20-24 (5 days)
        # Week 5: Jul 27-31 (5 days)
        # Total working days = 23 days
        assert get_working_days_for_month("2026-07", "2026-01-05") == 23

        # Mid-month joiner: joined 2026-07-13 (Monday)
        # Remaining working days: Weeks 3, 4, 5 = 15 days
        assert get_working_days_for_month("2026-07", "2026-07-13") == 15

        # Joined after month end:
        assert get_working_days_for_month("2026-07", "2026-08-01") == 0

    def test_r8_round_half_up(self):
        assert round_half_up(9.116, 2) == 9.12
        assert round_half_up(0.12345, 4) == 0.1235
        assert round_half_up(None, 2) is None


# =========================================================================== #
# 2. API Contract & Validation Tests
# =========================================================================== #
class TestAPIValidation:
    @patch("app.main.client")
    def test_health_success(self, mock_client):
        mock_client.admin.command.return_value = {"ok": 1}
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @patch("app.main.client")
    def test_health_database_down(self, mock_client):
        mock_client.admin.command.side_effect = Exception("Connection lost")
        resp = client.get("/health")
        assert resp.status_code == 503
        assert "detail" in resp.json()

    def test_create_employee_invalid_shift(self):
        # shift_start == shift_end must fail with 422
        payload = {
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        }
        resp = client.post("/employees", json=payload)
        assert resp.status_code == 422

    def test_create_employee_invalid_emp_code(self):
        # emp_code must match ^EMP\d{4,6}$
        payload = {
            "emp_code": "INVALID_CODE",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        resp = client.post("/employees", json=payload)
        assert resp.status_code == 422

    def test_create_employee_invalid_email(self):
        payload = {
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "not-an-email",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        resp = client.post("/employees", json=payload)
        assert resp.status_code == 422

    @patch("app.main.db")
    def test_create_employee_duplicate(self, mock_db):
        mock_db.employees.insert_one.side_effect = DuplicateKeyError("Duplicate")
        payload = {
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        resp = client.post("/employees", json=payload)
        assert resp.status_code == 409

    def test_punch_in_seconds_timestamp_rejected(self):
        # Seconds-looking epoch (below 100000000000) must be rejected with 422
        resp = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1700000000},
        )
        assert resp.status_code == 422

    def test_punch_in_invalid_status_rejected(self):
        # Only PRESENT, WFH, ON_DUTY allowed
        resp = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "status": "ABSENT"},
        )
        assert resp.status_code == 422

    @patch("app.main.db")
    def test_punch_in_unknown_employee(self, mock_db):
        mock_db.employees.find_one.return_value = None
        resp = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9999"},
        )
        assert resp.status_code == 404

    @patch("app.main.db")
    def test_punch_in_duplicate(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.insert_one.side_effect = DuplicateKeyError("Duplicate")
        resp = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1783312500000},
        )
        assert resp.status_code == 409

    @patch("app.main.db")
    def test_punch_out_unknown_employee(self, mock_db):
        mock_db.employees.find_one.return_value = None
        resp = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9999"},
        )
        assert resp.status_code == 404

    @patch("app.main.db")
    def test_punch_out_no_punch_in(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_db.attendance_logs.find_one.return_value = None
        resp = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": 1783312500000},
        )
        assert resp.status_code == 404

    @patch("app.main.db")
    def test_punch_out_already_punched_out(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy_id",
            "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc),
        }
        resp = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": 1783340000000},
        )
        assert resp.status_code == 409

    @patch("app.main.db")
    def test_punch_out_invalid_timing(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        pi = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy_id",
            "punch_in": pi,
            "punch_out": None,
        }
        # punched_at earlier than punch_in -> 422
        early_ms = int((pi - timedelta(hours=1)).timestamp()) * 1000
        resp = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": early_ms},
        )
        assert resp.status_code == 422

        # punched_at > 24 hours after punch_in -> 422
        late_ms = int((pi + timedelta(hours=25)).timestamp()) * 1000
        resp2 = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": late_ms},
        )
        assert resp2.status_code == 422

    def test_list_attendance_date_validation(self):
        # date_from > date_to must yield 422
        resp = client.get(
            "/attendance",
            params={"date_from": "2026-07-10", "date_to": "2026-07-05"},
        )
        assert resp.status_code == 422

    @patch("app.main.db")
    def test_regularize_changes_nothing(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc),
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        # Request changes nothing
        payload = {
            "reason": "Routine check",
            "regularized_by": "admin",
            "status": "PRESENT",
        }
        resp = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
        assert resp.status_code == 422

    @patch("app.main.db")
    def test_regularize_absent_with_punch_in_fails(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
        }
        payload = {
            "reason": "Change to absent",
            "regularized_by": "admin",
            "status": "ABSENT",
            "punch_in": 1783312500000,
        }
        resp = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
        assert resp.status_code == 422

    def test_department_trend_range_exceeds_92_days(self):
        resp = client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-01-01", "to": "2026-05-01"},
        )
        assert resp.status_code == 422

    def test_department_trend_to_before_from(self):
        resp = client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-07-10", "to": "2026-07-05"},
        )
        assert resp.status_code == 422

    def test_admin_explain_missing_required_params(self):
        # employee_monthly requires emp_code and month
        resp = client.get("/admin/explain/employee_monthly")
        assert resp.status_code == 422

        # department_summary requires month
        resp2 = client.get("/admin/explain/department_summary")
        assert resp2.status_code == 422

        # department_trend requires department, from, to
        resp3 = client.get("/admin/explain/department_trend")
        assert resp3.status_code == 422

    @patch("app.main.db")
    def test_punch_in_success(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.insert_one.return_value = MagicMock()
        resp = client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1783312500000, "status": "PRESENT"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] == 1783312500000
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert "id" not in data
        assert "_id" not in data

    @patch("app.main.db")
    def test_punch_out_success(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        pi = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        po = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy_id",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "punch_in": pi,
            "punch_out": None,
        }
        mock_db.attendance_logs.find_one_and_update.return_value = {
            "_id": "dummy_id",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": pi,
            "punch_out": po,
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        resp = client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": int(po.timestamp()) * 1000},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"] == 9.12
        assert data["half_day"] is False
        assert data["punch_out"] == int(po.timestamp()) * 1000

    @patch("app.main.db")
    def test_regularize_success(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        old_pi = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
        new_pi = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
        po = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "dummy_id",
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": old_pi,
            "punch_out": po,
            "work_hours": 8.42,
            "late_minutes": 35,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_db.attendance_logs.find_one_and_update.return_value = {
            "_id": "dummy_id",
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": new_pi,
            "punch_out": po,
            "work_hours": 9.03,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [
                {
                    "at": datetime(2026, 7, 8, 5, 0, 0, tzinfo=timezone.utc),
                    "by": "hr.admin",
                    "reason": "biometric glitch",
                    "changes": {
                        "punch_in": {"from": old_pi, "to": new_pi},
                        "late_minutes": {"from": 35, "to": 0},
                        "work_hours": {"from": 8.42, "to": 9.03},
                    },
                }
            ],
        }

        resp = client.patch(
            "/attendance/EMP0001/2026-07-07",
            json={
                "reason": "biometric glitch",
                "regularized_by": "hr.admin",
                "punch_in": int(new_pi.timestamp()) * 1000,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) == 1
        assert data["history"][0]["by"] == "hr.admin"
        assert data["history"][0]["changes"]["late_minutes"]["to"] == 0


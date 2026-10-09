from datetime import datetime
import unittest

from pydantic import ValidationError

from app import main


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TimeRuleTests(unittest.TestCase):
    def test_epoch_milliseconds_truncate_to_whole_seconds(self) -> None:
        instant = main.epoch_ms_to_datetime(1783312500999)
        self.assertEqual(instant.microsecond, 0)
        self.assertEqual(main.datetime_to_epoch_ms(instant), 1783312500000)

    def test_late_grace_is_strictly_more_than_ten_minutes(self) -> None:
        self.assertEqual(
            main.compute_late_minutes(utc("2026-07-06T04:10:00Z"), "2026-07-06", "09:30"),
            0,
        )
        self.assertEqual(
            main.compute_late_minutes(utc("2026-07-06T04:10:01Z"), "2026-07-06", "09:30"),
            10,
        )

    def test_overnight_punch_before_shift_end_belongs_to_previous_day(self) -> None:
        instant = utc("2026-07-07T00:29:00Z")  # 05:59 IST on July 7
        self.assertEqual(
            main.attendance_day(instant, "22:00", "06:00").isoformat(), "2026-07-06"
        )

    def test_overtime_requires_thirty_whole_minutes_and_supports_overnight(self) -> None:
        self.assertEqual(
            main.compute_overtime_minutes(
                utc("2026-07-06T13:29:59Z"), "2026-07-06", "09:30", "18:30"
            ),
            0,
        )
        self.assertEqual(
            main.compute_overtime_minutes(
                utc("2026-07-06T13:30:00Z"), "2026-07-06", "09:30", "18:30"
            ),
            30,
        )
        self.assertEqual(
            main.compute_overtime_minutes(
                utc("2026-07-07T01:10:00Z"), "2026-07-06", "22:00", "06:00"
            ),
            40,
        )

    def test_work_hours_use_half_up_before_half_day_comparison(self) -> None:
        hours = main.compute_work_hours(
            utc("2026-07-06T00:00:00Z"), utc("2026-07-06T04:29:42Z")
        )
        self.assertEqual(hours, 4.5)
        self.assertFalse(hours < 4.50)

    def test_weekday_count_and_join_date_window(self) -> None:
        self.assertEqual(main.weekday_count(main.Date(2026, 7, 1), main.Date(2026, 7, 31)), 23)
        self.assertEqual(main.weekday_count(main.Date(2026, 7, 16), main.Date(2026, 7, 31)), 12)
        self.assertEqual(main.weekday_count(main.Date(2026, 8, 1), main.Date(2026, 7, 31)), 0)


class RequestValidationTests(unittest.TestCase):
    def test_punch_in_rejects_seconds_floats_and_absence_status(self) -> None:
        invalid_payloads = (
            {"emp_code": "EMP0001", "punched_at": 1783312500},
            {"emp_code": "EMP0001", "punched_at": 1783312500000.0},
            {"emp_code": "EMP0001", "status": "ABSENT"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                main.PunchInIn.model_validate(payload)

    def test_employee_requires_a_valid_shift_and_calendar_date(self) -> None:
        valid = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        }
        self.assertEqual(main.EmployeeIn.model_validate(valid).shift_start, "09:30")
        for changes in (
            {"emp_code": "E1"},
            {"joined_on": "2026-02-30"},
            {"shift_end": "09:30"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                main.EmployeeIn.model_validate({**valid, **changes})

    def test_regularization_ignores_derived_values_but_rejects_null_status(self) -> None:
        request = main.RegularizeIn.model_validate(
            {
                "reason": "Corrected punch time",
                "regularized_by": "hr.admin",
                "late_minutes": 0,
            }
        )
        self.assertNotIn("late_minutes", request.model_dump())
        with self.assertRaises(ValidationError):
            main.RegularizeIn.model_validate(
                {"status": None, "reason": "Valid reason", "regularized_by": "hr"}
            )


class PipelineShapeTests(unittest.TestCase):
    def test_leaderboard_ranks_before_cutoff_filter(self) -> None:
        pipeline = main.late_leaderboard_pipeline("2026-07-01", "2026-07-31", None, 2)
        rank_index = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
        cutoff_index = next(
            i for i, stage in enumerate(pipeline) if stage.get("$match", {}).get("rank")
        )
        self.assertLess(rank_index, cutoff_index)
        self.assertIn("$rank", pipeline[rank_index]["$setWindowFields"]["output"]["rank"])

    def test_trend_builds_days_and_window_in_mongodb(self) -> None:
        pipeline = main.department_trend_pipeline(
            "Engineering", main.Date(2026, 7, 1), main.Date(2026, 7, 3)
        )
        serialized = repr(pipeline)
        self.assertIn("$dateDiff", serialized)
        self.assertIn("$range", serialized)
        self.assertIn("$setWindowFields", serialized)
        self.assertIn("_daily_stats", serialized)
        self.assertIn("documents", serialized)

    def test_department_summary_keeps_employee_rows_and_looks_up_logs(self) -> None:
        pipeline = main.department_summary_pipeline("2026-07-01", "2026-07-31", None)
        self.assertEqual(pipeline[0]["$match"], {"joined_on": {"$lte": "2026-07-31"}})
        self.assertEqual(pipeline[1]["$lookup"]["from"], "attendance_logs")


if __name__ == "__main__":
    unittest.main()

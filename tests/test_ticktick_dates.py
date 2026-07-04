"""Tests for TickTick date normalization and filtering — pure functions."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tools.ticktick_tool import _filter_by_date, _normalize_due_date

NY = ZoneInfo("America/New_York")


class TestNormalizeDueDate:
    def test_date_only_gets_9am_local_converted_to_utc(self):
        # July = EDT (UTC-4): 9:00 local -> 13:00 UTC
        assert _normalize_due_date("2026-07-04", tz=NY) == "2026-07-04T13:00:00.000+0000"

    def test_date_only_in_winter_uses_est(self):
        # December = EST (UTC-5): 9:00 local -> 14:00 UTC
        assert _normalize_due_date("2026-12-04", tz=NY) == "2026-12-04T14:00:00.000+0000"

    def test_naive_datetime_interpreted_in_user_tz(self):
        assert _normalize_due_date("2026-07-04T10:00:00", tz=NY) == "2026-07-04T14:00:00.000+0000"

    def test_naive_datetime_without_seconds(self):
        assert _normalize_due_date("2026-07-04T10:00", tz=NY) == "2026-07-04T14:00:00.000+0000"

    def test_negative_utc_offset_is_respected(self):
        # This input broke the old string-splitting parser
        assert (
            _normalize_due_date("2026-07-04T10:00:00-04:00", tz=NY)
            == "2026-07-04T14:00:00.000+0000"
        )

    def test_z_suffix(self):
        assert (
            _normalize_due_date("2026-07-04T22:30:00Z", tz=NY)
            == "2026-07-04T22:30:00.000+0000"
        )

    def test_ticktick_format_passes_through(self):
        already = "2026-07-04T13:00:00.000+0000"
        assert _normalize_due_date(already, tz=NY) == already

    def test_unparseable_returns_original(self):
        assert _normalize_due_date("next tuesday", tz=NY) == "next tuesday"

    def test_empty_returns_empty(self):
        assert _normalize_due_date("", tz=NY) == ""


class TestFilterByDate:
    NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    TASKS = [
        {"title": "due-today", "dueDate": "2026-07-04T09:00:00+00:00", "status": 0},
        {"title": "due-tomorrow", "dueDate": "2026-07-05T09:00:00+00:00", "status": 0},
        {"title": "overdue", "dueDate": "2026-07-01T09:00:00+00:00", "status": 0},
        {"title": "completed-overdue", "dueDate": "2026-07-01T09:00:00+00:00", "status": 2},
        {"title": "undated", "status": 0},
        {"title": "next-month", "dueDate": "2026-08-15T09:00:00+00:00", "status": 0},
    ]

    def _titles(self, date_range):
        return {t["title"] for t in _filter_by_date(self.TASKS, date_range, now=self.NOW)}

    def test_all_returns_everything(self):
        assert self._titles("all") == {t["title"] for t in self.TASKS}

    def test_today_is_due_today_or_earlier_without_undated(self):
        assert self._titles("today") == {"due-today", "overdue", "completed-overdue"}

    def test_week_includes_next_7_days_and_undated(self):
        assert self._titles("week") == {
            "due-today", "due-tomorrow", "overdue", "completed-overdue", "undated"
        }

    def test_overdue_excludes_completed_and_undated(self):
        # due-today at 09:00 is also past due at 12:00
        assert self._titles("overdue") == {"due-today", "overdue"}

    def test_ticktick_offset_format_parses(self):
        tasks = [{"title": "t", "dueDate": "2026-07-01T09:00:00.000+0000", "status": 0}]
        assert [t["title"] for t in _filter_by_date(tasks, "overdue", now=self.NOW)] == ["t"]

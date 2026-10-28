import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import Forbidden, TimedOut

from notifications import NotificationStore, NotificationWorker, REMINDERS, notification_card
from vc_client import VCClient, VCError, parse_due_date

NOW = 1_800_000_000


def event(due=NOW + 10 * 86400, cmid=10, submitted=False):
    return {"id": cmid, "name": "<Homework>", "course": "Course & test", "due": due, "submitted": submitted}


def store(tmp_path):
    value = NotificationStore(tmp_path / "notifications.sqlite3", tmp_path / "alerts.key")
    value.enable(7, 10, "university-user", "private-password")
    return value


def jobs(store, kind=None):
    with store.db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM notifications ORDER BY id")]
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_baseline_is_one_digest_not_a_burst_of_old_assignments(tmp_path):
    s = store(tmp_path)
    s.observe(7, [event(), event(NOW - 100, cmid=11)], NOW)
    assert [r["kind"] for r in jobs(s)] == ["baseline"]
    s.observe(7, [event(), event(NOW - 100, cmid=11)], NOW + 300)
    assert len(jobs(s)) == 1


def test_new_assignment_and_no_date_assignment_announced_once(tmp_path):
    s = store(tmp_path)
    s.observe(7, [], NOW)
    values = [event(NOW + 1000), event(None, cmid=11)]
    s.observe(7, values, NOW + 300)
    s.observe(7, values, NOW + 600)
    assert len(jobs(s, "new")) == 2
    assert not [r for r in jobs(s) if r["kind"].startswith("reminder:")]


def test_all_five_reminder_thresholds_once_across_restarts(tmp_path):
    s = store(tmp_path)
    e = event()
    s.observe(7, [e], NOW)
    for hours, _ in REMINDERS:
        when = e["due"] - hours * 3600
        s.observe(7, [e], when - 1)
        s.observe(7, [e], when + 1)
        s = NotificationStore(s.path, tmp_path / "alerts.key")
        s.observe(7, [e], when + 300)
    reminders = [r for r in jobs(s) if r["kind"].startswith("reminder:")]
    assert {r["kind"] for r in reminders} == {f"reminder:{h}" for h, _ in REMINDERS}
    assert len(reminders) == 5


def test_outage_sends_only_nearest_crossed_reminder(tmp_path):
    s = store(tmp_path)
    e = event()
    s.observe(7, [e], NOW)
    s.observe(7, [e], e["due"] - 11 * 3600)
    assert [r["kind"] for r in jobs(s) if r["kind"].startswith("reminder:")] == ["reminder:12"]


def test_undelivered_older_reminders_are_replaced_by_nearest_milestone(tmp_path):
    s = store(tmp_path)
    e = event()
    s.observe(7, [e], NOW)
    s.observe(7, [e], e["due"] - 7 * 86400 + 1)
    assert jobs(s, "reminder:168")[0]["state"] == "pending"
    s.observe(7, [e], e["due"] - 11 * 3600)
    assert jobs(s, "reminder:168")[0]["state"] == "cancelled"
    assert jobs(s, "reminder:12")[0]["state"] == "pending"


def test_due_changes_cancel_old_schedule_and_reschedule_reminders(tmp_path):
    s = store(tmp_path)
    e = event()
    s.observe(7, [e], NOW)
    s.observe(7, [e], e["due"] - 7 * 86400 + 1)
    later = event(e["due"] + 7 * 86400)
    s.observe(7, [later], e["due"] - 7 * 86400 + 300)
    s.observe(7, [later], e["due"] - 7 * 86400 + 600)
    assert len(jobs(s, "changed")) == 1
    assert jobs(s, "reminder:168")[0]["state"] == "cancelled"
    s.observe(7, [later], later["due"] - 7 * 86400 + 1)
    reminders = jobs(s, "reminder:168")
    assert [r["revision"] for r in reminders] == [1, 2]
    assert reminders[1]["state"] == "pending"


def test_date_added_removed_and_returned_are_distinct_changes(tmp_path):
    s = store(tmp_path)
    s.observe(7, [event(None)], NOW)
    for index, due in enumerate([NOW + 86400, None, NOW + 86400]):
        s.observe(7, [event(due)], NOW + 300 * (index + 1))
    assert len(jobs(s, "changed")) == 3
    assert json.loads(jobs(s, "changed")[0]["payload"])["previous_due"] is None


@pytest.mark.parametrize("case", ["submitted", "missing", "overdue"])
def test_reminders_stop_for_submitted_missing_and_overdue_assignments(tmp_path, case):
    s = store(tmp_path)
    e = event()
    s.observe(7, [e], NOW)
    s.observe(7, [e], e["due"] - 7 * 86400 + 1)
    values = [] if case == "missing" else [event(e["due"], submitted=case == "submitted")]
    now = e["due"] + 1 if case == "overdue" else e["due"] - 7 * 86400 + 300
    s.observe(7, values, now)
    assert all(r["state"] == "cancelled" for r in jobs(s) if r["kind"].startswith("reminder:"))


def test_encrypted_login_persists_and_disable_removes_secret(tmp_path):
    s = store(tmp_path)
    s = NotificationStore(s.path, tmp_path / "alerts.key")
    assert s.credentials(s.subscription(7)) == ["university-user", "private-password"]
    assert b"private-password" not in s.path.read_bytes()
    assert b"university-user" not in s.path.read_bytes()
    s.disable(7)
    assert s.subscription(7)["secret"] is None
    assert not s.subscription(7)["enabled"]


def test_missing_encryption_key_never_silently_replaces_existing_key(tmp_path):
    s = store(tmp_path)
    (tmp_path / "alerts.key").unlink()
    with pytest.raises(RuntimeError, match="key missing"):
        NotificationStore(s.path, tmp_path / "alerts.key")


def test_account_switch_discards_old_snapshots_and_queued_alerts(tmp_path):
    s = store(tmp_path)
    s.observe(7, [event()], NOW)
    s.enable(7, 99, "another-account", "another-password")
    assert jobs(s) == []
    assert not s.subscription(7)["initialized"]


def test_failed_scans_preserve_last_snapshot_and_warn_once_per_outage(tmp_path):
    s = store(tmp_path)
    s.observe(7, [event()], NOW)
    for _ in range(5):
        s.scan_failed(7)
    assert len(jobs(s, "sync_error")) == 1
    assert s.subscription(7)["last_scan"] == NOW
    s.observe(7, [event()], NOW + 1000)
    assert s.subscription(7)["failures"] == 0
    assert jobs(s, "sync_error")[0]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_outbox_retries_failure_and_does_not_repeat_acknowledged_delivery(tmp_path):
    s = store(tmp_path)
    s.observe(7, [], NOW)
    telegram = SimpleNamespace(send_message=AsyncMock(side_effect=[TimedOut(), None]))
    worker = NotificationWorker(s, telegram)
    await worker.deliver()
    assert jobs(s)[0]["state"] == "pending"
    with s.db() as db:
        db.execute("UPDATE notifications SET next_try=0")
    await NotificationWorker(s, telegram).deliver()
    await worker.deliver()
    assert telegram.send_message.await_count == 2
    assert jobs(s)[0]["state"] == "sent"


@pytest.mark.asyncio
async def test_blocked_bot_disables_background_login(tmp_path):
    s = store(tmp_path)
    s.observe(7, [], NOW)
    worker = NotificationWorker(s, SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked"))))
    await worker.deliver()
    assert not s.subscription(7)["enabled"]
    assert s.subscription(7)["secret"] is None


@pytest.mark.asyncio
async def test_inflight_scan_cannot_write_old_accounts_events_after_switch(tmp_path, monkeypatch):
    s = store(tmp_path)
    async def scan():
        s.enable(7, 99, "other", "other-password")
        return [event()]
    client = SimpleNamespace(account_id=10, notification_assignments=scan, close=AsyncMock())
    monkeypatch.setattr("notifications.VCClient", lambda u, p: client)
    worker = NotificationWorker(s, None)
    await worker.scan(s.subscription(7))
    assert not s.subscription(7)["initialized"]
    assert jobs(s) == []


@pytest.mark.parametrize("date", ["یکشنبه، 10 آبان 1405، 11:59 عصر", "یکشنبه، ۱۰ آبان ۱۴۰۵، ۱۱:۵۹ عصر"])
def test_persian_deadline_converts_to_verified_tehran_timestamp(date):
    assert parse_due_date(date) == 1793564940
    assert parse_due_date("-") is None
    with pytest.raises(VCError):
        parse_due_date("unexpected date")


@pytest.mark.asyncio
async def test_course_index_scan_handles_empty_courses_and_submitted_work():
    def handler(request):
        if "id=2" in str(request.url):
            return httpx.Response(200, text='<body id="page-mod-assign-index"><div class="alert-danger">هیچ موردی از تکالیف در این درس وجود ندارد</div></body>')
        return httpx.Response(200, text='''<body id="page-mod-assign-index"><table class="generaltable"><tbody><tr>
        <td>Week</td><td><a href="/mod/assign/view.php?id=42">Homework</a></td>
        <td>یکشنبه، 10 آبان 1405، 11:59 عصر</td><td>برای تصحیح تحویل داده شده است</td><td>-</td>
        </tr></tbody></table></body>''')
    client = VCClient("test", "test")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.ensure_login = AsyncMock()
    client.ajax = AsyncMock(return_value={"courses": [{"id": 1, "fullname": "Course 1"}, {"id": 2, "fullname": "Empty course"}]})
    try:
        events = await client.notification_assignments()
        assert len(events) == 1
        assert events[0]["due"] == 1793564940 and events[0]["submitted"]
    finally:
        await client.close()


def test_notification_content_escapes_names_and_shows_old_and_new_dates():
    text, buttons = notification_card("changed", {"event": event(), "previous_due": None})
    assert "&lt;Homework&gt;" in text and "<Homework>" not in text
    assert "Previous: No due date" in text and "Tehran" in text
    assert buttons.inline_keyboard[0][0].callback_data == "open:10"

"""Persistent assignment snapshots, deadline reminders, encrypted opt-in logins and outbox."""
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from cryptography.fernet import Fernet
from telegram.error import Forbidden, RetryAfter, TelegramError

import ui
from vc_client import VCClient

POLL_SECONDS = 300
REMINDERS = ((168, "1 week"), (72, "3 days"), (48, "2 days"), (24, "1 day"), (12, "12 hours"))


class NotificationStore:
    def __init__(self, path, key_path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key_path = Path(key_path)
        if not key_path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                table = db.execute("SELECT name FROM sqlite_master WHERE name='subscriptions'").fetchone()
                if table and db.execute("SELECT 1 FROM subscriptions WHERE secret IS NOT NULL LIMIT 1").fetchone():
                    raise RuntimeError("Notification key missing; restore the existing key before starting.")
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    owner INTEGER PRIMARY KEY, account TEXT NOT NULL, secret BLOB,
                    enabled INTEGER NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
                    last_scan REAL, failures INTEGER NOT NULL DEFAULT 0, last_success REAL);
                CREATE TABLE IF NOT EXISTS assignments (
                    owner INTEGER, account TEXT, cmid INTEGER, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(owner,account,cmid));
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY, owner INTEGER, account TEXT, cmid INTEGER,
                    revision INTEGER, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0,
                    UNIQUE(owner,account,cmid,revision,kind));
            ''')

    def db(self):
        # context manager commits and closes; SQLite's own context only commits.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            with closing(sqlite3.connect(self.path)) as db, db:
                db.row_factory = sqlite3.Row
                yield db
        return connection()

    def subscription(self, owner):
        with self.db() as db:
            row = db.execute("SELECT * FROM subscriptions WHERE owner=?", (owner,)).fetchone()
        return dict(row) if row else None

    def enable(self, owner, account_id, username, password):
        account = hashlib.sha256(f"kntu:{account_id}".encode()).hexdigest()
        secret = self.cipher.encrypt(json.dumps([username, password]).encode())
        old = self.subscription(owner)
        with self.db() as db:
            if old and old["account"] != account:
                db.execute("DELETE FROM assignments WHERE owner=?", (owner,))
                db.execute("DELETE FROM notifications WHERE owner=?", (owner,))
                db.execute("DELETE FROM subscriptions WHERE owner=?", (owner,))
            db.execute('''INSERT INTO subscriptions(owner,account,secret,enabled) VALUES(?,?,?,1)
                ON CONFLICT(owner) DO UPDATE SET secret=excluded.secret,enabled=1,failures=0''',
                       (owner, account, secret))
        return account

    def disable(self, owner):
        with self.db() as db:
            db.execute("UPDATE subscriptions SET enabled=0,secret=NULL WHERE owner=?", (owner,))
            db.execute("UPDATE notifications SET state='cancelled' WHERE owner=? AND state!='sent'", (owner,))

    def credentials(self, subscription):
        return json.loads(self.cipher.decrypt(subscription["secret"]))

    @staticmethod
    def enqueue(db, owner, account, cmid, revision, kind, payload):
        db.execute('''INSERT OR IGNORE INTO notifications(owner,account,cmid,revision,kind,payload)
                      VALUES(?,?,?,?,?,?)''', (owner, account, cmid, revision, kind, json.dumps(payload)))

    def observe(self, owner, events, now, announce_baseline=True):
        """Commit one complete scan and its alerts atomically. Failed scans never call this."""
        sub = self.subscription(owner)
        if not sub or not sub["enabled"]:
            return
        account = sub["account"]
        with self.db() as db:
            # Mark missing/hidden assignments inactive; their reminders must stop.
            db.execute("UPDATE assignments SET active=0 WHERE owner=? AND account=?", (owner, account))
            db.execute("UPDATE notifications SET state='cancelled' WHERE owner=? AND kind='sync_error' AND state='pending'", (owner,))
            for event in events:
                cmid = event["id"]
                old = db.execute("SELECT * FROM assignments WHERE owner=? AND account=? AND cmid=?", (owner, account, cmid)).fetchone()
                old_event = json.loads(old["payload"]) if old else None
                changed = old_event is not None and old_event["due"] != event["due"]
                revision = old["revision"] + int(changed) if old else 1
                db.execute('''INSERT INTO assignments VALUES(?,?,?,?,?,1)
                    ON CONFLICT(owner,account,cmid) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,active=1''',
                           (owner, account, cmid, json.dumps(event), revision))
                if changed:
                    db.execute("UPDATE notifications SET state='cancelled' WHERE owner=? AND account=? AND cmid=? AND state='pending'", (owner, account, cmid))
                    self.enqueue(db, owner, account, cmid, revision, "changed", {"event": event, "previous_due": old_event["due"]})
                elif old is None and sub["initialized"]:
                    self.enqueue(db, owner, account, cmid, revision, "new", {"event": event})
                # First scan/new assignment/changed date: announce once, never burst
                # through reminder milestones that had already passed before observation.
                if old is not None and not changed and sub["last_scan"] is not None and event["due"] and event["due"] > now and not event.get("submitted"):
                    crossed = [(h, label) for h, label in REMINDERS if sub["last_scan"] < event["due"] - h * 3600 <= now]
                    if crossed:
                        hours, label = crossed[-1]  # nearest relevant milestone after downtime
                        db.execute("UPDATE notifications SET state='cancelled' WHERE owner=? AND account=? AND cmid=? AND revision=? AND kind LIKE 'reminder:%' AND state='pending'",
                                   (owner, account, cmid, revision))
                        self.enqueue(db, owner, account, cmid, revision, f"reminder:{hours}", {"event": event, "milestone": label})
            # Cancel stale reminders for disappeared, submitted or overdue work.
            for row in db.execute("SELECT * FROM notifications WHERE owner=? AND kind LIKE 'reminder:%' AND state='pending'", (owner,)).fetchall():
                assignment = db.execute("SELECT * FROM assignments WHERE owner=? AND account=? AND cmid=?", (owner, account, row["cmid"])).fetchone()
                event = json.loads(assignment["payload"]) if assignment else None
                if not assignment or not assignment["active"] or event.get("submitted") or not event["due"] or event["due"] <= now or assignment["revision"] != row["revision"]:
                    db.execute("UPDATE notifications SET state='cancelled' WHERE id=?", (row["id"],))
            if not sub["initialized"] and announce_baseline:
                self.enqueue(db, owner, account, 0, 1, "baseline", {"events": events})
            db.execute("UPDATE subscriptions SET initialized=1,last_scan=?,last_success=?,failures=0 WHERE owner=?", (now, now, owner))

    def scan_failed(self, owner):
        with self.db() as db:
            db.execute("UPDATE subscriptions SET failures=failures+1 WHERE owner=?", (owner,))
            sub = db.execute("SELECT * FROM subscriptions WHERE owner=?", (owner,)).fetchone()
            if sub and sub["enabled"] and sub["failures"] >= 3:
                # One warning per outage, reset after a later successful scan.
                self.enqueue(db, owner, sub["account"], 0, int(sub["last_success"] or 0), "sync_error", {})


def due_text(value):
    if not value:
        return "No due date"
    return datetime.fromtimestamp(value, ui.TZ).strftime("%a, %d %b %Y · %H:%M Tehran")


def notification_card(kind, payload):
    if kind == "sync_error":
        return ("⚠️ <b>Assignment alerts need attention</b>\n\nMoodle could not be checked for several attempts. Alerts may be delayed. Reconnect your university account and enable alerts again if this persists.",
                ui.markup([ui.button("Alert settings", "notifications", "primary")]))
    if kind == "baseline":
        events = payload["events"]
        upcoming = sorted((e for e in events if e["due"] and e["due"] > time.time() and not e.get("submitted")), key=lambda e: e["due"])
        text = f"🔔 <b>Assignment alerts are on</b>\n\nTracking {len(events)} existing assignments across your courses. New assignments and deadline changes will be announced from now on.\n\nReminders: <b>1 week · 3 days · 2 days · 1 day · 12 hours</b> before the deadline."
        if upcoming:
            text += "\n\n<b>Coming up</b>\n" + "\n".join(f"• {ui.e(e['name'])} — {due_text(e['due'])}" for e in upcoming[:5])
        return text, ui.markup([ui.button("Browse assignments", "list:upcoming:0", "primary"), ui.button("Alert settings", "notifications")])
    event = payload["event"]
    if kind == "new":
        title, extra = "🆕 New assignment", ""
    elif kind == "changed":
        title = "🗓 Due date updated"
        extra = f"\nPrevious: {due_text(payload['previous_due'])}"
    else:
        title = "⏰ Assignment reminder"
        extra = f"\nMilestone: {ui.e(payload['milestone'])} before the deadline"
    text = f"<b>{title}</b>\n\n<b>{ui.e(event['name'])}</b>\n{ui.e(event['course'])}\n\nDue: {due_text(event['due'])}{extra}"
    return text, ui.markup([ui.button("Open assignment →", f"open:{event['id']}", "primary")], [ui.button("Alert settings", "notifications")])


class NotificationWorker:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        self.clients = {}
        self.wake = asyncio.Event()
        self.next_scan = {}
        self.baseline_overview = set()

    def changed(self, owner):
        self.next_scan.pop(owner, None)
        self.wake.set()

    async def scan(self, sub):
        owner = sub["owner"]
        username, password = self.store.credentials(sub)
        fingerprint = hashlib.sha256(sub["secret"]).hexdigest()
        old = self.clients.get(owner)
        if old and old[0] != fingerprint:
            await old[1].close()
            old = None
        if not old:
            self.clients[owner] = (fingerprint, VCClient(username, password))
        client = self.clients[owner][1]
        events = await client.notification_assignments()
        account = hashlib.sha256(f"kntu:{client.account_id}".encode()).hexdigest()
        if account != sub["account"]:
            raise RuntimeError("Authenticated notification account changed")
        current = self.store.subscription(owner)
        if not current or not current["enabled"] or current["secret"] != sub["secret"]:
            return  # Account/preferences changed while the scan awaited Moodle.
        self.store.observe(owner, events, time.time(), announce_baseline=owner in self.baseline_overview)
        self.baseline_overview.discard(owner)

    async def deliver(self):
        with self.store.db() as db:
            rows = db.execute('''SELECT n.* FROM notifications n JOIN subscriptions s ON n.owner=s.owner AND n.account=s.account
                WHERE n.state='pending' AND s.enabled=1 AND n.next_try<=? ORDER BY n.id LIMIT 30''', (time.time(),)).fetchall()
        for row in rows:
            current = self.store.subscription(row["owner"])
            if not current or not current["enabled"] or current["account"] != row["account"]:
                continue
            payload = json.loads(row["payload"])
            if row["kind"].startswith("reminder:") and payload["event"]["due"] <= time.time():
                with self.store.db() as db:
                    db.execute("UPDATE notifications SET state='cancelled' WHERE id=?", (row["id"],))
                continue
            text, buttons = notification_card(row["kind"], payload)
            try:
                # Notifications contain only escaped HTML and are bounded by formatting.
                for chunk in ui.html_chunks(text):
                    await self.bot.send_message(row["owner"], chunk, parse_mode="HTML", reply_markup=buttons)
            except Forbidden:
                self.store.disable(row["owner"])
                continue
            except TelegramError as error:
                delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
                if isinstance(error, RetryAfter):
                    delay = error.retry_after.total_seconds() if hasattr(error.retry_after, "total_seconds") else error.retry_after
                with self.store.db() as db:
                    db.execute("UPDATE notifications SET attempts=attempts+1,next_try=? WHERE id=?", (time.time() + delay, row["id"]))
                continue
            with self.store.db() as db:
                db.execute("UPDATE notifications SET state='sent' WHERE id=?", (row["id"],))

    async def run(self):
        try:
            while True:
                self.wake.clear()
                with self.store.db() as db:
                    subscriptions = [dict(r) for r in db.execute("SELECT * FROM subscriptions WHERE enabled=1")]
                enabled = {s["owner"] for s in subscriptions}
                for owner in list(self.clients):
                    if owner not in enabled:
                        await self.clients.pop(owner)[1].close()
                for sub in subscriptions:
                    owner = sub["owner"]
                    if time.monotonic() >= self.next_scan.get(owner, 0):
                        try:
                            await self.scan(sub)
                        except Exception:
                            self.store.scan_failed(owner)  # Never log credentials/response bodies.
                        self.next_scan[owner] = time.monotonic() + POLL_SECONDS
                try:
                    await self.deliver()
                except Exception:
                    # Keep monitoring alive; never print a Telegram request URL or credentials.
                    print("Notification delivery check failed; retrying later.", flush=True)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
        finally:
            for _, client in self.clients.values():
                await client.close()

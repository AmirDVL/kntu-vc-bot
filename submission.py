"""Durable, account-scoped receipts and read-only recovery after ambiguous writes."""
import asyncio
import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

from vc_client import VCError, file_manifest, utc_now


UNRESOLVED = {"uploading", "saving", "finalizing", "verifying", "unknown"}


class ReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, owner INTEGER, account TEXT, cmid INTEGER, payload TEXT)")

    def put(self, owner, account, receipt):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (receipt["id"], owner, account, receipt["cmid"], json.dumps(receipt, ensure_ascii=False)))

    def latest(self, owner, account, cmid):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT payload FROM receipts WHERE owner=? AND account=? AND cmid=? ORDER BY rowid DESC LIMIT 1",
                             (owner, account, cmid)).fetchone()
        return json.loads(row[0]) if row else None


class SubmissionService:
    def __init__(self, client, store, owner):
        self.client, self.store, self.owner = client, store, owner
        if not getattr(client, "account_id", None):
            raise VCError("Reconnect first: an authenticated Moodle account ID is required for verification receipts.")
        self.account = hashlib.sha256(f"kntu:{client.account_id}".encode()).hexdigest()

    def latest(self, cmid):
        return self.store.latest(self.owner, self.account, cmid)

    def check_pending(self, cmid):
        old = self.latest(cmid)
        if old and old["state"] in UNRESOLVED:
            raise VCError(f"An earlier attempt has an uncertain outcome. Use /verify {cmid}. No new upload was sent.")

    async def prepare(self, cmid, paths):
        self.check_pending(cmid)
        detail = await self.client.detail(cmid)
        before = await self.client.remote_manifest(detail)
        expected = before + file_manifest(paths)
        if len({f["name"] for f in expected}) != len(expected):
            raise VCError("Duplicate names in the current submission and staged files.")
        return {"expected": expected, "baseline": before, "staged": file_manifest(paths)}

    async def prepare_final(self, cmid):
        self.check_pending(cmid)
        previous = self.latest(cmid)
        expected = previous["expected"] if previous else await self.client.remote_manifest(await self.client.detail(cmid))
        await self.client.verify_submission(cmid, expected)
        return expected

    async def _run(self, cmid, expected, operation, perform):
        self.check_pending(cmid)
        receipt = {"id": secrets.token_hex(8), "cmid": cmid, "operation": operation,
                   "state": "prepared", "started_at": utc_now(), "expected": expected, "uploads": []}

        def progress(state, **fields):
            receipt["state"] = state
            if "upload" in fields:
                receipt["uploads"].append(fields.pop("upload"))
            receipt.update(fields)
            receipt["updated_at"] = utc_now()
            self.store.put(self.owner, self.account, receipt)

        progress("prepared")
        try:
            result = await perform(progress)
        except (httpx.HTTPError, VCError, ValueError):
            # Never replay a write. Recover exclusively using fresh GET/downloads.
            if receipt["state"] in {"saving", "finalizing", "verifying"}:
                progress("unknown")
                for delay in (0, 1, 2):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        result = await self.client.verify_submission(cmid, expected)
                        break
                    except (httpx.HTTPError, VCError, ValueError) as error:
                        progress("unknown", verification_issue=str(error) if isinstance(error, VCError)
                                 else "Network/server response prevented verification.")
                        continue
                else:
                    return receipt
            else:
                progress("failed")
                raise
        state = result["submission_state"]
        if state not in {"draft", "submitted"}:
            state = "unknown"
        # A final-submit timeout followed by a draft is an unresolved write,
        # even though the file bytes are confirmed intact.
        if operation == "finalize" and state != "submitted":
            state = "unknown"
        progress(state, verification_issue="" if state != "unknown" else "Moodle has not confirmed the required submission state.",
                 verified_at=result["checked_at"], server_date=result["server_date"],
                 server_modified=result["modified"], server_timing=result["timing"],
                 moodle_state=result["submission_state"], integrity_verified=True)
        return receipt

    async def save(self, cmid, paths, declaration, reviewed):
        if file_manifest(paths) != reviewed["staged"]:
            raise VCError("Staged file bytes changed since review. Request /review again.")
        baseline = await self.client.remote_manifest(await self.client.detail(cmid))
        if baseline != reviewed["baseline"]:
            raise VCError("Moodle's files changed since review. Request /review again.")
        return await self._run(cmid, reviewed["expected"], "save",
                               lambda progress: self.client.save(cmid, paths, declaration, reviewed["expected"], progress))

    async def finalize(self, cmid, statement, expected):
        return await self._run(cmid, expected, "finalize",
                               lambda progress: self.client.finalize(cmid, statement, expected, progress))

    async def verify(self, cmid):
        receipt = self.latest(cmid)
        if not receipt:
            raise VCError("No receipt exists for this account and assignment. No known uploaded bytes can be certified.")
        try:
            result = await self.client.verify_submission(cmid, receipt["expected"])
        except (httpx.HTTPError, VCError, ValueError) as error:
            receipt.update(state="unknown", updated_at=utc_now(), integrity_verified=False,
                           verification_issue=str(error) if isinstance(error, VCError)
                           else "Network/server response prevented verification.")
            self.store.put(self.owner, self.account, receipt)
            return receipt
        state = result["submission_state"]
        if state not in {"draft", "submitted"} or (receipt["operation"] == "finalize" and state != "submitted"):
            state = "unknown"
        receipt.update(state=state, moodle_state=result["submission_state"], integrity_verified=True,
                       verification_issue="" if state != "unknown" else "Moodle has not confirmed the required submission state.",
                       verified_at=result["checked_at"], updated_at=utc_now(), server_date=result["server_date"],
                       server_modified=result["modified"], server_timing=result["timing"])
        self.store.put(self.owner, self.account, receipt)
        return receipt

"""Private-chat Telegram interface, with isolated in-memory university sessions."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from vc_client import VCClient, VCError, settings
from submission import ReceiptStore, SubmissionService

ROOT = Path(__file__).resolve().parent
DOWNLOAD_LIMIT = 20 * 1024**2  # Telegram's hosted getFile endpoint.
SEND_LIMIT = 50 * 1024**2
TTL = 30 * 60


@dataclass
class UserSession:
    client: VCClient
    touched: float = field(default_factory=time.monotonic)
    cmid: int | None = None
    folder: Path | None = None
    files: list[Path] = field(default_factory=list)
    confirm: str | None = None
    statement: str | None = None
    final_cmid: int | None = None
    save_declaration: str = ""
    reviewed: dict | None = None
    final_files: list | None = None
    confirm_action: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def clear_upload(self):
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)
        self.folder, self.cmid, self.files, self.confirm = None, None, [], None
        self.statement, self.final_cmid = None, None
        self.save_declaration = ""
        self.reviewed, self.final_files, self.confirm_action = None, None, None


sessions: dict[int, UserSession] = {}
connecting: dict[int, tuple[str | None, float]] = {}


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise VCError("Please give the document a valid filename.")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name[:180]


def private(update):
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


async def reply(update, text, **kwargs):
    # Always plain text: university content must not become Telegram markup.
    for start in range(0, max(len(text), 1), 3900):
        await update.effective_message.reply_text(text[start:start + 3900], **(kwargs if start + 3900 >= len(text) else {}))


def session(update):
    uid = update.effective_user.id
    if uid not in sessions:
        raise VCError("Use /connect to sign in to your university account first.")
    value = sessions[uid]
    if time.monotonic() - value.touched > TTL:
        raise VCError("Your session expired. Use /connect again.")
    value.touched = time.monotonic()
    return value


def cmid_arg(context):
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        raise VCError("Include an assignment ID, for example /assignment 123.")
    return int(context.args[0])


def submission_service(update, context, s):
    return SubmissionService(s.client, context.application.bot_data["receipts"], update.effective_user.id)


def receipt_text(receipt):
    state = receipt["state"]
    if state == "submitted" and receipt.get("integrity_verified"):
        verdict = "TURNED IN — VERIFIED: Moodle reports submitted and the downloaded file bytes match."
    elif state == "draft" and receipt.get("integrity_verified"):
        verdict = "FILES SAVED AS DRAFT — NOT TURNED IN. A separate final submission is required."
    else:
        verdict = "OUTCOME UNCERTAIN — TURN-IN IS NOT VERIFIED. Do not assume success or re-upload blindly."
    def local(value):
        if not value:
            return "not available"
        return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Tehran")).isoformat(timespec="milliseconds")
    lines = [verdict, f"Receipt: {receipt['id']}", f"Assignment: {receipt['cmid']}",
             f"Attempt started (Tehran): {local(receipt['started_at'])}"]
    for file in receipt.get("uploads", []):
        lines.append(f"Draft upload {file['name']} (Tehran): request started {local(file['started_at'])}; "
                     f"acknowledged {local(file['acknowledged_at'])}; round trip {file.get('round_trip_ms', 'unknown')} ms.")
    if receipt.get("save_requested_at"):
        lines.append(f"Save requested (Tehran): {local(receipt['save_requested_at'])}")
    if receipt.get("submit_requested_at"):
        lines.append(f"Final submit requested (Tehran): {local(receipt['submit_requested_at'])}")
    for prefix, title in (("save", "Save"), ("submit", "Final submit")):
        if receipt.get(f"{prefix}_acknowledged_at"):
            lines.append(f"{title} response received (Tehran): {local(receipt[f'{prefix}_acknowledged_at'])}")
    lines.append(f"Last successful byte check (Tehran): {local(receipt.get('verified_at'))}")
    lines.append(f"Moodle state at last successful byte check: {receipt.get('moodle_state', 'unknown')}")
    if receipt.get("verification_issue"):
        lines.append(f"Verification issue: {receipt['verification_issue']}")
    if receipt.get("server_modified"):
        lines.append(f"Moodle last modified: {receipt['server_modified']}")
    if receipt.get("server_timing"):
        lines.append(f"Moodle timing status: {receipt['server_timing']}")
    if receipt.get("server_date"):
        lines.append(f"Verification response Date header: {receipt['server_date']}")
    lines.append("Client timestamps record requests/checks, not the exact instant Moodle accepted a submission.")
    lines.append("Expected files (bytes and SHA-256):")
    for file in receipt["expected"]:
        lines.append(f"{file['name']} — {file['size']:,} bytes\n{file['sha256']}")
    if state == "draft":
        lines.append(f"/finalize {receipt['cmid']}")
    lines.append(f"Recheck safely: /verify {receipt['cmid']}")
    lines.append(f"https://vc.kntu.ac.ir/mod/assign/view.php?id={receipt['cmid']}")
    return "\n\n".join(lines)


async def start(update, context):
    if not private(update):
        return
    await reply(update, "University homework bot\n\n"
                "/connect — sign in to your own university account\n"
                "/login — admin's account from .env\n"
                "/due — timeline across all courses (includes overdue)\n"
                "/assignment ID — description, dates, status, file list\n"
                "/files ID — download assignment attachments\n"
                "/upload ID — view limits and prepare homework\n"
                "Send documents, then /review to confirm saving.\n"
                "/finalize ID — review Moodle's separate final submission\n"
                "/verify ID — check stored file fingerprints and turn-in status again\n"
                "/cancel — discard staged files\n"
                "/logout — remove session and staged files\n\n"
                "Send homework as documents to preserve filenames and formats. "
                "Sessions expire after 30 minutes of inactivity; restart requires reconnecting.")


async def connect(update, context):
    if not private(update):
        return
    connecting[update.effective_user.id] = (None, time.monotonic())
    await reply(update, "Send your university username next, then your password when prompted. "
                "Credentials pass through Telegram's bot chat (not end-to-end encrypted). "
                "I delete your credential messages and keep the session only in memory. /cancel to stop.")


async def establish(update, username, password):
    client = VCClient(username, password)
    try:
        await client.login()
    except Exception:
        await client.close()
        raise VCError("Sign-in failed. Check your credentials or use Moodle in a browser if it needs extra verification.") from None
    uid = update.effective_user.id
    old = sessions.pop(uid, None)
    if old:
        old.clear_upload()
        await old.client.close()
    sessions[uid] = UserSession(client)
    await reply(update, "Signed in to your university account. Use /due.")


async def login(update, context):
    if not private(update):
        return
    if update.effective_user.id != context.application.bot_data["admin_id"]:
        await reply(update, "Use /connect to sign in to your own account.")
        return
    username, password, _ = settings()
    await establish(update, username, password)


async def text(update, context):
    if not private(update):
        return
    uid = update.effective_user.id
    pending = connecting.get(uid)
    if not pending:
        await reply(update, "Use /start for commands. Send homework as a document after /upload ID.")
        return
    try:
        await update.effective_message.delete()
    except Exception:
        connecting.pop(uid, None)
        await reply(update, "Could not delete your message. Please delete it yourself. Sign-in cancelled.")
        return
    username, began = pending
    if time.monotonic() - began > 300:
        connecting.pop(uid, None)
        raise VCError("Sign-in timed out. Use /connect again.")
    if username is None:
        connecting[uid] = (update.effective_message.text.strip(), began)
        await reply(update, "Now send your university password. I will delete that message too.")
    else:
        connecting.pop(uid, None)
        await establish(update, username, update.effective_message.text)


async def due(update, context):
    s = session(update)
    async with s.lock:
        events = await s.client.assignments()
    if not events:
        await reply(update, "No due assignments appear in your Moodle action timeline. "
                    "Assignments without due dates or already completed may be absent.")
        return
    now = time.time()
    lines = ["Moodle assignment timeline (Tehran time):"]
    for e in events:
        date = datetime.fromtimestamp(e["due"], ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
        lines.append(f"\n{'Overdue' if e['due'] < now else 'Due'} {date}\n{e['name']}\n{e['course']}\n/assignment {e['id']}")
    await reply(update, "\n".join(lines))


async def assignment(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        d = await s.client.detail(cmid)
        try:
            limits = (await s.client.rules(cmid)).summary()
        except VCError as e:
            limits = str(e)
    files = "\n".join(f"• {f['name']}" for f in d["attachments"]) or "No attached files."
    await reply(update, f"{d['name']}\n{d['dates']}\n\n{d['description']}\n\n{d['status']}\n\n{limits}\n\n{files}\n\n/files {cmid}\n/upload {cmid}\n{d['url']}")


async def files(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        detail = await s.client.detail(cmid)
        if not detail["attachments"]:
            await reply(update, "No attached files.")
            return
        for attachment in detail["attachments"]:
            with tempfile.TemporaryDirectory(prefix="vc-download-") as directory:
                dest = Path(directory) / safe_name(attachment["name"])
                async with s.client.http.stream("GET", s.client.trusted(attachment["url"])) as response:
                    response.raise_for_status()
                    size = 0
                    with dest.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > SEND_LIMIT:
                                break
                            handle.write(chunk)
                if size > SEND_LIMIT:
                    await reply(update, f"{attachment['name']} exceeds Telegram's hosted sending limit. Download it from Moodle:\n{detail['url']}")
                elif "text/html" in response.headers.get("content-type", ""):
                    raise VCError("Download returned a web page instead of a file. Use /login or /connect again.")
                else:
                    with dest.open("rb") as handle:
                        await update.effective_message.reply_document(handle, filename=dest.name, read_timeout=120, write_timeout=120)


async def upload(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        submission_service(update, context, s).check_pending(cmid)
        rules = await s.client.rules(cmid)
        detail = await s.client.detail(cmid)
        s.clear_upload()
        s.cmid = cmid
        s.folder = Path(tempfile.mkdtemp(prefix="vc-upload-"))
    await reply(update, f"Preparing: {detail['name']}\n{rules.summary()}\n\n"
                "Existing Moodle files will be preserved. Send your homework as documents. "
                "Telegram's hosted bot service lets me download files up to 20 MiB each, even if Moodle allows more. "
                "Then /review; /cancel discards the staged files. Nothing is saved to Moodle until you confirm.")


async def document(update, context):
    if not private(update):
        return
    s = session(update)
    async with s.lock:
        if not s.cmid or not s.folder:
            raise VCError("Choose an assignment with /upload ID first.")
        doc = update.effective_message.document
        if doc.file_size is None or doc.file_size > DOWNLOAD_LIMIT:
            raise VCError("Telegram's hosted download limit is 20 MiB per document. Use Moodle for larger files.")
        if len(s.files) >= 20:
            raise VCError("Bot staging limit is 20 files. Use the Moodle browser for larger batches.")
        name = safe_name(doc.file_name or "homework")
        path = s.folder / name
        if path.exists():
            raise VCError("That filename is already staged. Rename it or /cancel and start again.")
        rules = await s.client.rules(s.cmid)
        if rules.max_bytes > 0 and doc.file_size > rules.max_bytes:
            raise VCError("This file exceeds the assignment's size limit.")
        telegram_file = await context.bot.get_file(doc.file_id)
        try:
            await telegram_file.download_to_drive(path)
            if path.stat().st_size != doc.file_size:
                raise VCError("Telegram download byte count did not match. The file was discarded; send it again.")
            rules.validate([*s.files, path])
        except Exception:
            path.unlink(missing_ok=True)
            raise
        s.files.append(path)
        s.confirm = None  # Any change invalidates an earlier review button.
        s.reviewed = None
    await reply(update, f"Staged {name} ({doc.file_size:,} bytes). {len(s.files)} file(s) ready. /review when finished.")


async def review(update, context):
    s = session(update)
    async with s.lock:
        if not s.cmid or not s.files:
            raise VCError("Use /upload ID, then send at least one document.")
        rules = await s.client.rules(s.cmid)
        rules.validate(s.files)
        if len(rules.declaration) > 2000:
            raise VCError("The submission declaration is too long to review here. Please use Moodle.")
        s.save_declaration = rules.declaration
        s.reviewed = await submission_service(update, context, s).prepare(s.cmid, s.files)
        s.confirm = secrets.token_hex(8)
        s.confirm_action = "save"
        listing = "\n".join(f"• {f['name']} ({f['size']:,} bytes)\nSHA-256: {f['sha256']}" for f in s.reviewed["staged"])
        declaration = f"\n\nBy saving, you accept this Moodle statement:\n{rules.declaration}" if rules.declaration else ""
        await reply(update, f"Save to assignment {s.cmid}?\n{listing}\n\n{rules.summary()}\n"
                    f"This saves files on Moodle. Some assignments require an additional final submission.{declaration}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Save homework to Moodle", callback_data=f"save:{s.confirm}")]]))


async def finalize(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        form = await s.client.final_form(cmid)
        s.final_files = await submission_service(update, context, s).prepare_final(cmid)
        if len(form["statement"]) > 3400:
            raise VCError("Submission declaration is too long to display safely here. Please finalize in Moodle.")
        s.statement, s.final_cmid = form["statement"], cmid
        s.confirm = secrets.token_hex(8)
        s.confirm_action = "final"
        listing = "\n".join(f"• {f['name']} ({f['size']:,} bytes)\nSHA-256: {f['sha256']}" for f in s.final_files)
        await reply(update, f"Final submission for assignment {cmid}:\n\n{form['statement']}\n\n"
                    f"{listing}\n\nConfirm only if these are the correct files and you accept the statement above. Final submission may lock further edits.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Accept statement and submit", callback_data=f"final:{s.confirm}")]]))


async def callback(update, context):
    if not private(update):
        return
    await update.callback_query.answer()
    s = session(update)
    async with s.lock:
        action, token = update.callback_query.data.split(":", 1)
        if not s.confirm or not secrets.compare_digest(token, s.confirm) or action != s.confirm_action:
            raise VCError("This confirmation has expired. Request /review or /finalize again.")
        s.confirm = None
        service = submission_service(update, context, s)
        if action == "save":
            if not s.reviewed:
                raise VCError("No reviewed file fingerprints. Request /review again.")
            receipt = await service.save(s.cmid, s.files, s.save_declaration, s.reviewed)
            if receipt["state"] in {"draft", "submitted"}:
                s.clear_upload()
            await reply(update, receipt_text(receipt))
        elif action == "final" and s.statement and s.final_cmid:
            receipt = await service.finalize(s.final_cmid, s.statement, s.final_files)
            s.statement, s.final_cmid = None, None
            await reply(update, receipt_text(receipt))
        await update.callback_query.edit_message_reply_markup(reply_markup=None)


async def verify(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        receipt = await submission_service(update, context, s).verify(cmid)
    await reply(update, receipt_text(receipt))


async def cancel(update, context):
    if not private(update):
        return
    uid = update.effective_user.id
    connecting.pop(uid, None)
    if uid in sessions:
        sessions[uid].clear_upload()
    await reply(update, "Cancelled. Staged files removed.")


async def logout(update, context):
    if not private(update):
        return
    connecting.pop(update.effective_user.id, None)
    s = sessions.pop(update.effective_user.id, None)
    if s:
        s.clear_upload()
        await s.client.close()
    await reply(update, "Session and staged files removed from this bot.")


async def on_error(update, context):
    # Do not log Update/exception objects: either may contain passwords or tokens.
    if isinstance(update, Update) and private(update):
        message = str(context.error) if isinstance(context.error, VCError) else "Request failed. Check Moodle before retrying a submission."
        await reply(update, message)


def guarded(handler):
    async def run(update, context):
        if private(update):
            await handler(update, context)
    return run


async def reap(application):
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for uid, s in list(sessions.items()):
            if now - s.touched > TTL and not s.lock.locked():
                sessions.pop(uid, None)
                s.clear_upload()
                await s.client.close()
        for uid, (_, began) in list(connecting.items()):
            if now - began > 300:
                connecting.pop(uid, None)


async def init(application):
    application.bot_data["reaper"] = asyncio.create_task(reap(application))
    print("Telegram connection initialized; starting polling.", flush=True)


async def shutdown(application):
    application.bot_data["reaper"].cancel()
    for s in sessions.values():
        s.clear_upload()
        await s.client.close()
    sessions.clear()


def main():
    import os
    os.chdir(ROOT)
    _, _, config = settings()
    token = config.get("TELEGRAM_BOT_TOKEN") or config.get("Token")
    admin = config.get("TELEGRAM_USER_ID") or config.get("AdminID")
    if not token or not admin or not str(admin).isdigit():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID (or Token and AdminID) in .env.")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    application = (Application.builder().token(token).post_init(init).post_shutdown(shutdown).build())
    application.bot_data["admin_id"] = int(admin)
    application.bot_data["receipts"] = ReceiptStore(ROOT / ".data" / "receipts.sqlite3")
    for name, handler in {"start": start, "help": start, "connect": connect, "login": login, "due": due,
                          "assignment": assignment, "files": files, "upload": upload, "review": review,
                          "finalize": finalize, "verify": verify, "cancel": cancel, "logout": logout}.items():
        application.add_handler(CommandHandler(name, guarded(handler)))
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^(save|final):[0-9a-f]{16}$"))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, document))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text))
    application.add_error_handler(on_error)
    print("Bot starting. Credentials and Telegram messages are not logged.")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

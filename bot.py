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
from telegram import BotCommand, LinkPreviewOptions, MenuButtonCommands, ReplyKeyboardRemove, Update
from telegram.error import BadRequest
import ui
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
    upload_name: str = ""
    upload_nonce: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def clear_upload(self):
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)
        self.folder, self.cmid, self.files, self.confirm = None, None, [], None
        self.statement, self.final_cmid = None, None
        self.save_declaration = ""
        self.upload_name, self.upload_nonce = "", ""
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


async def card(update, text, buttons=None, edit=True):
    parts = ui.html_chunks(text)
    for index, part in enumerate(parts):
        options = dict(parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        if index == len(parts) - 1 and buttons:
            options["reply_markup"] = buttons
        if edit and update.callback_query and len(parts) == 1:
            try:
                await update.callback_query.edit_message_text(part, **options)
            except BadRequest as error:
                if "message is not modified" not in str(error).lower():
                    raise
        else:
            await update.effective_message.reply_text(part, **options)


async def busy(update, text):
    return await update.effective_message.reply_text(text)


async def home(update, context):
    uid = update.effective_user.id
    s = sessions.get(uid)
    active = s is not None and time.monotonic() - s.touched <= TTL
    if active:
        s.touched = time.monotonic()
        text = "🎓 <b>KNTU · Coursework</b>\n\nYour university account is connected.\nBrowse assignments, download materials, or continue your homework upload."
        rows = [[ui.button("📚 Browse assignments", "list:upcoming:0", "primary")]]
        if s.cmid:
            text += f"\n\n📎 <b>Upload in progress</b>\n{ui.e(s.upload_name)} · {len(s.files)} file(s) staged"
            rows.append([ui.button("Continue upload →", "staging", "primary")])
        rows += [[ui.button("Help", "help"), ui.button("Account", "account")]]
    else:
        text = "🎓 <b>KNTU · Coursework</b>\n\nYour assignments, files, and homework submissions—in one place.\n\nConnect your own university account to get started."
        rows = [[ui.button("Connect university account →", "connect", "primary")]]
        if uid == context.application.bot_data["admin_id"]:
            rows.append([ui.button("Quick sign in · admin", "login")])
        rows.append([ui.button("How it works", "help")])
    await card(update, text, ui.markup(*rows))


async def help_screen(update, context):
    await card(update, "❓ <b>From assignment to turn-in</b>\n\n"
               "<b>1 · Browse</b>\nOpen Assignments and choose upcoming or overdue work.\n\n"
               "<b>2 · Prepare</b>\nRead the description and limits. Tap Add homework, then attach files as <b>documents</b> to preserve their names and formats.\n\n"
               "<b>3 · Review &amp; save</b>\nCheck the assignment and file list before confirming. The bot downloads Moodle’s saved files to check their actual bytes.\n\n"
               "<b>4 · Turn in</b>\nA saved draft still needs final submission. Only a verified submitted status gets a green confirmation. An uncertain result offers a safe recheck.\n\n"
               "<b>Good to know</b>\nTelegram allows transfers up to 20 MiB per incoming document. University sessions expire after 30 minutes of inactivity. Receipt details stay available after reconnecting.\n\n"
               "Commands still work: /due · /upload ID · /review · /verify ID · /cancel · /logout",
               ui.markup([ui.button("⌂ Home", "home", "primary")]))


async def account(update, context):
    session(update)
    await card(update, "👤 <b>Your university session</b>\n\nConnected to your own Moodle account.\nSessions expire after 30 minutes of inactivity. Signing out removes this session and any staged files; verification receipts remain available after reconnecting.",
               ui.markup([ui.button("Sign out…", "logout_prompt", "danger")], [ui.button("‹ Home", "home")]))


async def staging(update, context):
    s = session(update)
    if not s.cmid:
        await card(update, "📎 <b>No upload in progress</b>\n\nChoose an assignment, then tap <b>Add homework</b>.",
                   ui.markup([ui.button("Browse assignments →", "list:upcoming:0", "primary")]))
        return
    listing = ui.file_list([{"name": p.name, "size": p.stat().st_size} for p in s.files])
    text = f"📎 <b>Your homework upload</b>\n\n<b>{ui.e(s.upload_name)}</b>\nAssignment {s.cmid} · {len(s.files)} file(s) staged\n\n"
    text += listing or "Attach your first file as a document."
    text += "\n\nSend more documents, or review the files below. <b>Staged files have not been saved to Moodle.</b>"
    rows = []
    if s.files:
        rows.append([ui.button("Review homework →", "review", "primary")])
        for i, path in enumerate(s.files):
            rows.append([ui.button(f"✕ Remove {path.name[:40]}", f"remove:{s.upload_nonce}:{i}")])
    rows += [[ui.button("Assignment details", f"open:{s.cmid}")], [ui.button("Discard staged files…", "discard_prompt", "danger")]]
    await card(update, text, ui.markup(*rows))


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
    await reply(update, "🎓 KNTU VC · Your coursework, closer.", reply_markup=ui.keyboard())
    await home(update, context)


async def connect(update, context):
    if not private(update):
        return
    connecting[update.effective_user.id] = (None, time.monotonic())
    await reply(update, "🔐 Connect · Step 1 of 2\n\nSend your university username or email.\n\nCredential messages are deleted and your session stays in memory. Telegram bot chats are not end-to-end encrypted.\n\n/cancel to stop.", reply_markup=ReplyKeyboardRemove())


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
    await reply(update, "✓ University account connected.", reply_markup=ui.keyboard())


async def login(update, context):
    if not private(update):
        return
    if update.effective_user.id != context.application.bot_data["admin_id"]:
        await reply(update, "Use /connect to sign in to your own account.")
        return
    username, password, _ = settings()
    status = await busy(update, "🔐 Connecting to your university account…")
    await establish(update, username, password)
    await status.edit_text("✓ Connected to your university account.")
    await home(update, context)


async def text(update, context):
    if not private(update):
        return
    uid = update.effective_user.id
    pending = connecting.get(uid)
    navigation = {ui.HOME: home, ui.ASSIGNMENTS: due, ui.UPLOAD: staging, ui.HELP: help_screen}
    if update.effective_message.text in navigation:
        connecting.pop(uid, None)
        await navigation[update.effective_message.text](update, context)
        return
    if not pending:
        await card(update, "Choose an action below. To add homework, open an assignment and tap <b>Add homework</b>.",
                   ui.markup([ui.button("📚 Assignments", "list:upcoming:0", "primary"), ui.button("⌂ Home", "home")]))
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
        await reply(update, "🔐 Connect · Step 2 of 2\n\nSend your university password. This message will also be deleted.\n\n/cancel to stop.")
    else:
        connecting.pop(uid, None)
        status = await busy(update, "🔐 Connecting to your university account…")
        await establish(update, username, update.effective_message.text)
        await status.edit_text("✓ Connected to your university account.")
        await home(update, context)


async def due(update, context, kind="upcoming", page=0):
    s = session(update)
    async with s.lock:
        events = await s.client.assignments()
    text, buttons = ui.assignment_list(events, kind, page, time.time())
    await card(update, text, buttons)


async def assignment(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        d = await s.client.detail(cmid)
        can_upload = True
        try:
            limits = ui.rules_text(await s.client.rules(cmid))
        except VCError as error:
            limits, can_upload = ui.e(str(error)), False
    state = {"submitted": "✅ Submitted", "draft": "🟠 Draft · final submission pending", "new": "⚪ No submission yet"}.get(d["submission_state"], ui.e(d.get("submission_label") or "Submission state unavailable · check Moodle"))
    text = f"📘 <b>{ui.e(d['name'])}</b>\n{ui.e(d.get('course', ''))}\n\n{ui.e(d['dates'])}\n\n<b>{state}</b>"
    if d.get("timing"):
        text += f"\n{ui.e(d['timing'])}"
    text += f"\n\n<b>Description</b>\n{ui.e(d['description'])}\n\n{limits}"
    if d["attachments"]:
        text += "\n\n<b>Course materials</b>\n" + "\n".join("📄 " + ui.e(f["name"]) for f in d["attachments"])
    rows = []
    if d["attachments"]:
        rows.append([ui.button(f"↓ Download materials ({len(d['attachments'])})", f"download:{cmid}")])
    if s.cmid == cmid:
        rows.append([ui.button("Continue homework upload →", "staging", "primary")])
    elif can_upload:
        rows.append([ui.button("＋ Add homework", f"upload:{cmid}", "primary")])
    if d["submission_state"] == "draft":
        rows.append([ui.button("Review final submission →", f"finish:{cmid}", "success")])
    if submission_service(update, context, s).latest(cmid):
        rows.append([ui.button("↻ Verify turn-in", f"check:{cmid}")])
    rows += [[ui.button("Open Moodle ↗", url=f"https://vc.kntu.ac.ir/mod/assign/view.php?id={cmid}")],
             [ui.button("‹ Assignments", "list:upcoming:0"), ui.button("⌂ Home", "home")]]
    await card(update, text, ui.markup(*rows))


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


async def upload(update, context, replace=False):
    s, cmid = session(update), cmid_arg(context)
    if s.cmid == cmid and not replace:
        await staging(update, context)
        return
    if s.cmid and not replace:
        await card(update, f"📎 <b>An upload is already in progress</b>\n\n{ui.e(s.upload_name)} · {len(s.files)} staged file(s).\nSwitching assignments discards these local files. Moodle files are unaffected.",
                   ui.markup([ui.button("Continue current upload", "staging", "primary")],
                             [ui.button("Discard & switch assignment", f"switch:{s.upload_nonce}:{cmid}", "danger")]))
        return
    async with s.lock:
        submission_service(update, context, s).check_pending(cmid)
        rules = await s.client.rules(cmid)
        detail = await s.client.detail(cmid)
        s.clear_upload()
        s.cmid = cmid
        s.upload_name = detail["name"]
        if detail.get("course"):
            s.upload_name += " · " + detail["course"]
        s.upload_nonce = secrets.token_hex(4)
        s.folder = Path(tempfile.mkdtemp(prefix="vc-upload-"))
    await card(update, f"📎 <b>Add your homework</b>\n\n<b>{ui.e(detail['name'])}</b>\n{ui.e(detail['dates'])}\n\n{ui.rules_text(rules)}\n\n<b>Next:</b> tap the paperclip and send your file as a <b>document</b>. You can send multiple documents.\n\nExisting Moodle files stay in place. Files are saved only after your review and confirmation.",
               ui.markup([ui.button("View staged files", "staging")], [ui.button("Cancel upload…", "discard_prompt", "danger")]))


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
        s.upload_nonce = secrets.token_hex(4)
    await card(update, f"📎 <b>File received</b>\n\n{ui.e(name)} · {ui.size(path.stat().st_size)}\n{len(s.files)} file(s) staged for <b>{ui.e(s.upload_name)}</b>.\n\nSend another document or review your homework. <b>Nothing has been saved to Moodle yet.</b>",
               ui.markup([ui.button("Review homework →", "review", "primary")], [ui.button("Manage staged files", "staging")]), edit=False)


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
        listing = ui.file_list(s.reviewed["staged"])
        declaration = f"\n\n<b>Moodle declaration</b>\n{ui.e(rules.declaration)}\nSaving accepts this statement." if rules.declaration else ""
        text = f"📝 <b>Review your homework</b>\n\n<b>{ui.e(s.upload_name)}</b>\nAssignment {s.cmid}\n\n{listing}\n\n{ui.rules_text(rules)}\n\nSaving sends these files to Moodle. If Moodle saves a draft, you will be prompted to finalize it.{declaration}"
        await card(update, text, ui.markup([ui.button("Save homework to Moodle", f"save:{s.confirm}", "success")],
                                         [ui.button("Edit files", "staging"), ui.button("File fingerprints", "fingerprints")],
                                         [ui.button("Discard…", "discard_prompt", "danger")]))


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
        listing = ui.file_list(s.final_files)
        d = await s.client.detail(cmid)
        await card(update, f"📤 <b>Final turn-in</b>\n\n<b>{ui.e(d['name'])}</b>\nAssignment {cmid}\n\n{listing}\n\n<b>Moodle declaration</b>\n{ui.e(form['statement'])}\n\nConfirm that these are the correct files and you accept this statement. <b>Final turn-in may lock further edits.</b>",
                   ui.markup([ui.button("Accept & turn in homework", f"final:{s.confirm}", "success")],
                             [ui.button("File fingerprints", "final_fingerprints"), ui.button("‹ Assignment", f"open:{cmid}")]))


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
        progress = await busy(update, "⏳ Sending homework to Moodle…\nI’ll check the stored file bytes and turn-in status before reporting the result.")
        try:
            if action == "save":
                if not s.reviewed:
                    raise VCError("No reviewed file fingerprints. Request /review again.")
                receipt = await service.save(s.cmid, s.files, s.save_declaration, s.reviewed)
                if receipt["state"] in {"draft", "submitted"}:
                    s.clear_upload()
            elif action == "final" and s.statement and s.final_cmid:
                receipt = await service.finalize(s.final_cmid, s.statement, s.final_files)
                s.statement, s.final_cmid = None, None
            else:
                raise VCError("This confirmation expired. Open the assignment to continue.")
        except Exception:
            await progress.edit_text("⚠️ This action did not complete with a verified result. Check the receipt or Moodle before retrying.")
            raise
        await progress.edit_text("✓ Moodle check complete." if receipt["state"] != "unknown" else "⚠️ Turn-in could not be verified. Use the safe recheck below.")
        await show_receipt(update, receipt)
        await update.callback_query.edit_message_reply_markup(reply_markup=None)



async def verify(update, context):
    s, cmid = session(update), cmid_arg(context)
    async with s.lock:
        receipt = await submission_service(update, context, s).verify(cmid)
    await show_receipt(update, receipt)


async def show_receipt(update, receipt):
    text, buttons = ui.receipt_card(receipt)
    await card(update, text, buttons, edit=False)


async def cancel(update, context):
    if not private(update):
        return
    uid = update.effective_user.id
    connecting.pop(uid, None)
    if uid in sessions:
        sessions[uid].clear_upload()
    await reply(update, "Upload cancelled. Staged files removed; Moodle files are unchanged.", reply_markup=ui.keyboard())
    await home(update, context)


async def logout(update, context):
    if not private(update):
        return
    connecting.pop(update.effective_user.id, None)
    s = sessions.pop(update.effective_user.id, None)
    if s:
        s.clear_upload()
        await s.client.close()
    await reply(update, "Signed out. Session and staged files removed.", reply_markup=ui.keyboard())
    await home(update, context)


async def on_error(update, context):
    # Do not log Update/exception objects: either may contain passwords or tokens.
    if isinstance(update, Update) and private(update):
        message = str(context.error) if isinstance(context.error, VCError) else "Request failed. Check Moodle before retrying a submission."
        await card(update, "⚠️ <b>Action needs attention</b>\n\n" + ui.e(message), ui.markup([ui.button("⌂ Home", "home"), ui.button("Connect account", "connect")]), edit=False)


async def receipt_details(update, context):
    s, cmid = session(update), cmid_arg(context)
    receipt = submission_service(update, context, s).latest(cmid)
    if not receipt:
        raise VCError("No verification receipt exists for this assignment yet.")
    await reply(update, receipt_text(receipt), reply_markup=ui.markup(
        [ui.button("↻ Verify again", f"check:{cmid}", "primary"), ui.button("‹ Assignment", f"open:{cmid}")]))


async def navigate(update, context):
    if not private(update):
        return
    query = update.callback_query
    data = query.data
    await query.answer("Checking Moodle…" if data.split(":")[0] in {
        "list", "open", "download", "upload", "finish", "check", "review"} else None)
    routes = {"home": home, "help": help_screen, "connect": connect, "login": login,
              "account": account, "staging": staging, "review": review, "cancel": cancel, "logout": logout}
    if data in routes:
        if data == "home":
            connecting.pop(update.effective_user.id, None)
        await routes[data](update, context)
        return
    if data == "logout_prompt":
        session(update)
        await card(update, "👤 <b>Sign out?</b>\n\nYour university session and staged files will be removed. Saved Moodle files and verification receipts are kept.",
                   ui.markup([ui.button("Sign out", "logout", "danger")], [ui.button("Keep me connected", "home", "primary")]))
        return
    if data == "discard_prompt":
        s = session(update)
        if not s.cmid:
            await staging(update, context)
            return
        await card(update, "🗑 <b>Discard staged homework?</b>\n\nThis removes only the local files waiting to be saved. It does not delete files already on Moodle.",
                   ui.markup([ui.button("Discard staged files", f"discard:{s.upload_nonce}", "danger")], [ui.button("Keep editing", "staging", "primary")]))
        return
    if data in {"fingerprints", "final_fingerprints"}:
        s = session(update)
        fingerprints = s.final_files if data == "final_fingerprints" else s.reviewed["staged"] if s.reviewed else None
        if not fingerprints:
            raise VCError("This review expired. Review your homework again.")
        await card(update, "🔎 <b>File fingerprints</b>\n\n" + ui.file_list(fingerprints, hashes=True),
                   ui.markup([ui.button("‹ Back to review", f"finish:{s.final_cmid}" if data == "final_fingerprints" else "review")]))
        return
    parts = data.split(":")
    if len(parts) == 2 and parts[0] == "discard":
        s = session(update)
        if not s.upload_nonce or not secrets.compare_digest(parts[1], s.upload_nonce):
            raise VCError("Your staged files changed. Open My upload before discarding them.")
        await cancel(update, context)
        return
    if len(parts) == 3 and parts[0] == "switch" and parts[2].isdigit():
        s = session(update)
        if not s.upload_nonce or not secrets.compare_digest(parts[1], s.upload_nonce):
            raise VCError("Your staged files changed. Choose the new assignment again.")
        original_args = context.args
        context.args = [parts[2]]
        try:
            await upload(update, context, replace=True)
        finally:
            context.args = original_args
        return
    if len(parts) == 3 and parts[0] == "list" and parts[1] in {"upcoming", "overdue"} and parts[2].isdigit():
        await due(update, context, parts[1], int(parts[2]))
        return
    if len(parts) == 3 and parts[0] == "remove" and parts[2].isdigit():
        s = session(update)
        async with s.lock:
            index = int(parts[2])
            if not s.upload_nonce or not secrets.compare_digest(parts[1], s.upload_nonce) or index >= len(s.files):
                raise VCError("This file list changed. Open My upload to see the current files.")
            path = s.files.pop(index)
            path.unlink(missing_ok=True)
            s.confirm, s.reviewed, s.confirm_action = None, None, None
            s.upload_nonce = secrets.token_hex(4)
        await staging(update, context)
        return
    handlers = {"open": assignment, "download": files, "upload": upload,
                "finish": finalize, "check": verify, "receipt": receipt_details}
    if len(parts) == 2 and parts[0] in handlers and parts[1].isdigit() and 0 < int(parts[1]) < 2**63:
        original_args = context.args
        context.args = [parts[1]]
        try:
            await handlers[parts[0]](update, context)
        finally:
            context.args = original_args
        return
    raise VCError("This button is no longer available. Open Home to continue.")


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
    await application.bot.set_my_commands([
        BotCommand("start", "Open your coursework dashboard"), BotCommand("due", "Browse upcoming and overdue assignments"),
        BotCommand("connect", "Connect your university account"), BotCommand("review", "Review staged homework"),
        BotCommand("cancel", "Discard staged files"), BotCommand("help", "How homework submission works"),
        BotCommand("logout", "Sign out of your university account")])
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await application.bot.set_my_short_description("Your KNTU assignments, files and verified homework submissions.")
    await application.bot.set_my_description(
        "Browse your KNTU assignments and download course materials without opening every course. "
        "Add homework, review the files and confirm saving. The bot checks Moodle's stored file bytes "
        "and distinguishes a draft from a verified final turn-in. Start below to connect your university account.")
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
    for name, handler in {"start": start, "menu": start, "help": help_screen, "connect": connect, "login": login, "due": due,
                          "assignment": assignment, "files": files, "upload": upload, "review": review,
                          "finalize": finalize, "verify": verify, "receipt": receipt_details, "cancel": cancel, "logout": logout}.items():
        application.add_handler(CommandHandler(name, guarded(handler)))
    application.add_handler(CallbackQueryHandler(navigate, pattern=r"^(?!save:|final:).+"))
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^(save|final):[0-9a-f]{16}$"))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, document))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, text))
    application.add_error_handler(on_error)
    print("Bot starting. Credentials and Telegram messages are not logged.")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

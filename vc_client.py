"""Session-based Moodle client. Never log credentials, cookies, or form tokens."""
from __future__ import annotations

import asyncio
import json
import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dotenv import dotenv_values

BASE = "https://vc.kntu.ac.ir"


def parse_due_date(text):
    """KNTU's Persian assignment-index dates; never silently guess an unknown date."""
    import jdatetime
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩يك", "01234567890123456789یک"))
    text = re.sub(r"[\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text).strip()
    if text in {"", "-", "–", "—", "بدون مهلت", "No due date", "None"}:
        return None
    months = "فروردین اردیبهشت خرداد تیر مرداد شهریور مهر آبان آذر دی بهمن اسفند".split()
    match = re.search(r"(\d{1,2})\s+(" + "|".join(months) + r")\s+(\d{4})[،,\s]+(\d{1,2}):(\d{2})\s*(صبح|عصر|بعدازظهر|قبل\s*ازظهر)?", text)
    if not match:
        raise VCError("Unrecognized Moodle due-date format; assignment alerts were not updated.")
    day, month, year, hour, minute, period = match.groups()
    hour = int(hour)
    if period:
        if not 1 <= hour <= 12:
            raise VCError("Invalid Moodle 12-hour due date.")
        hour = hour % 12 + (12 if period in {"عصر", "بعدازظهر"} else 0)
    try:
        gregorian = jdatetime.datetime(int(year), months.index(month) + 1, int(day), hour, int(minute)).togregorian()
        return int(gregorian.replace(tzinfo=ZoneInfo("Asia/Tehran")).timestamp())
    except ValueError:
        raise VCError("Invalid Moodle due date; assignment alerts were not updated.") from None


class VCError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def file_manifest(paths):
    result = []
    for path in paths:
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        result.append({"name": path.name, "size": path.stat().st_size, "sha256": digest})
    return result


def soup(html):
    return BeautifulSoup(html, "html.parser")


def form_data(form):
    return {i["name"]: i.get("value", "") for i in form.select("input[name]")
            if i.get("type") not in {"submit", "button", "file", "reset", "checkbox", "radio"}}


def js_objects(html, marker):
    """Decode JSON arguments emitted by Moodle, without executing server scripts."""
    decoder = json.JSONDecoder()
    for match in re.finditer(re.escape(marker), html):
        start = html.find("{", match.end())
        try:
            value, _ = decoder.raw_decode(html[start:])
            yield value
        except (ValueError, TypeError):
            continue


@dataclass
class Rules:
    max_files: int
    max_bytes: int
    accepted: list[str]
    existing: list[str]
    options: dict
    picker: dict
    fields: dict
    action: str
    declaration: str = ""
    checkboxes: dict | None = None

    def validate(self, paths: list[Path]):
        names = [p.name for p in paths]
        if len(set(names)) != len(names):
            raise VCError("Duplicate file names; rename the files first.")
        # Existing files are preserved. Replacements are deliberately unsupported.
        if set(names) & set(self.existing):
            raise VCError("A file with this name already exists on Moodle. Rename it or edit in the browser.")
        if self.max_files >= 0 and len(self.existing) + len(paths) > self.max_files:
            raise VCError(f"Maximum {self.max_files} files, including {len(self.existing)} existing files.")
        for path in paths:
            if self.max_bytes > 0 and path.stat().st_size > self.max_bytes:
                raise VCError(f"{path.name}: exceeds {self.max_bytes:,} bytes per file.")
            if self.accepted and "*" not in self.accepted:
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                allowed = any(path.name.lower().endswith(t.lower()) if t.startswith(".")
                              else mime.startswith(t[:-1]) if t.endswith("/*")
                              else mime == t for t in self.accepted)
                if not allowed:
                    raise VCError(f"{path.name}: allowed formats: {', '.join(self.accepted)}")
        area_limit = int(self.options.get("areamaxbytes", -1))
        existing_size = 0
        if area_limit > 0:
            for f in self.options.get("list", []):
                raw_size = f.get("filesize", f.get("size", 0))
                if not str(raw_size).isdigit():
                    raise VCError("Cannot determine existing file sizes. Please use Moodle for this submission.")
                existing_size += int(raw_size)
        if area_limit > 0 and existing_size + sum(p.stat().st_size for p in paths) > area_limit:
            raise VCError(f"Total files exceed the assignment's {area_limit:,}-byte storage limit.")

    def summary(self):
        size = f"{self.max_bytes / 1024**2:g} MiB/file" if self.max_bytes > 0 else "site limit"
        count = str(self.max_files) if self.max_files >= 0 else "unlimited"
        area = int(self.options.get("areamaxbytes", -1))
        total = f" Total storage: {area:,} bytes." if area > 0 else ""
        return f"Files: {count}; {size}; formats: {', '.join(self.accepted) or 'any'}. Existing: {len(self.existing)}.{total}"


class VCClient:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.http = httpx.AsyncClient(follow_redirects=True, timeout=60)
        self.lock = asyncio.Lock()
        self.sesskey = None
        self.account_id = None

    async def close(self):
        await self.http.aclose()

    def trusted(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"vc.kntu.ac.ir", "sso.kntu.ac.ir"}:
            raise VCError("Unexpected university URL; refusing to send account data.")
        return url

    async def get(self, url):
        response = await self.http.get(self.trusted(urljoin(BASE, url)), headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        return response

    async def login(self):
        response = await self.get("/login/index.php")
        doc = soup(response.text)
        link = doc.select_one('a[href*="/auth/oauth2/login.php"]')
        if link:
            response = await self.get(link["href"])
            doc = soup(response.text)
        form = doc.select_one("form#kc-form-login")
        if form:
            action = self.trusted(urljoin(str(response.url), form["action"]))
            fields = form_data(form)
            fields.update(username=self.username, password=self.password)
            response = await self.http.post(action, data=fields)
            response.raise_for_status()
        response = await self.get("/my/index.php")
        if urlparse(str(response.url)).hostname != "vc.kntu.ac.ir" or soup(response.text).select_one("form#kc-form-login, input#username"):
            raise VCError("University sign-in failed or requires browser interaction.")
        match = re.search(r'"sesskey"\s*:\s*"([^"\\]+)"', response.text)
        if not match:
            raise VCError("Cannot find authenticated Moodle session.")
        self.sesskey = match[1]
        config = next(js_objects(response.text, "M.cfg ="), {})
        self.account_id = config.get("userId")
        if not str(self.account_id).isdigit() or int(self.account_id) <= 0:
            self.sesskey = None
            raise VCError("Cannot identify the authenticated university account safely.")

    async def ensure_login(self):
        if not self.sesskey:
            await self.login()
            return
        response = await self.get("/my/index.php")
        if "/login/" in str(response.url) or "sso.kntu.ac.ir" in str(response.url):
            self.sesskey = None
            await self.login()

    async def ajax(self, method, args):
        response = await self.http.post(BASE + "/lib/ajax/service.php", params={"sesskey": self.sesskey},
                                        json=[{"index": 0, "methodname": method, "args": args}])
        response.raise_for_status()
        try:
            result = response.json()[0]
        except (ValueError, IndexError, KeyError):
            raise VCError("Moodle returned an unexpected response. Try /login again.") from None
        if result.get("error"):
            raise VCError("Moodle rejected the calendar request. Try /login again.")
        return result["data"]

    async def assignments(self):
        await self.ensure_login()
        events, after, seen = [], 0, set()
        for _ in range(100):
            data = await self.ajax("core_calendar_get_action_events_by_timesort", {
                "timesortfrom": 0, "limitnum": 50, "aftereventid": after})
            batch = data.get("events", [])
            if not batch:
                break
            for e in batch:
                if e.get("modulename") == "assign" and e.get("eventtype") == "due":
                    url = e.get("url", "")
                    if not url:
                        url = e.get("action", {}).get("url", "")
                    query = parse_qs(urlparse(url).query)
                    if "id" in query:
                        cmid = int(query["id"][0])
                        if cmid not in seen:
                            seen.add(cmid)
                            events.append({"id": cmid, "name": e.get("activityname") or e["name"],
                                           "course": e.get("course", {}).get("fullname", ""),
                                           "due": e.get("timestart", e.get("timesort", 0))})
            next_after = data.get("lastid") or batch[-1]["id"]
            if next_after == after:
                raise VCError("Calendar pagination stalled; refusing to return an incomplete list.")
            after = next_after
            if len(batch) < 50:
                break
        else:
            raise VCError("Calendar is too large; listing would be incomplete.")
        return sorted(events, key=lambda e: e["due"])

    async def notification_assignments(self):
        """Scan enrolled course indices, including no-date and completed assignments."""
        await self.ensure_login()
        courses, offset = [], 0
        for _ in range(100):
            data = await self.ajax("core_course_get_enrolled_courses_by_timeline_classification",
                                   {"classification": "all", "limit": 100, "offset": offset})
            batch = data.get("courses", [])
            courses.extend(batch)
            if len(batch) < 100:
                break
            next_offset = data.get("nextoffset")
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise VCError("Enrolled course pagination stalled. Alert scan was not saved.")
            offset = next_offset
        else:
            raise VCError("Too many enrolled courses; alert scan would be incomplete.")
        events = {}
        for course in courses:
            response = await self.get(f"/mod/assign/index.php?id={int(course['id'])}&lang=fa")
            doc = soup(response.text)
            if doc.select_one("form#kc-form-login, input#username") or "sso.kntu.ac.ir" in str(response.url):
                raise VCError("Assignment alert session expired during scan.")
            if not doc.body or doc.body.get("id") != "page-mod-assign-index":
                raise VCError("Unexpected course assignment page; alert scan was not saved.")
            for alert in doc.select(".alert-danger, .errorbox"):
                # Moodle renders an ordinary empty course with alert-danger too.
                message = alert.get_text(" ", strip=True)
                if not any(empty in message for empty in ("هیچ موردی از تکالیف در این درس وجود ندارد", "There are no assignments in this course")):
                    raise VCError("A course assignment index was unavailable; alert scan was not saved.")
            for row in doc.select("table.generaltable tbody tr"):
                a = row.select_one('a[href*="/mod/assign/view.php"]')
                if not a:
                    continue
                cells = row.find_all("td", recursive=False)
                if len(cells) < 4:
                    raise VCError("Unknown assignment index layout; alert scan was not saved.")
                cmid = int(parse_qs(urlparse(a["href"]).query)["id"][0])
                due = parse_due_date(cells[-3].get_text(" ", strip=True))
                status = cells[-2].get_text(" ", strip=True)
                submitted = status in {"برای تصحیح تحویل داده شده است", "Submitted for grading"} or bool(row.select_one(".submissionstatussubmitted"))
                events[cmid] = {"id": cmid, "name": a.get_text(" ", strip=True), "course": course["fullname"],
                                "due": due, "submitted": submitted}
        return list(events.values())

    async def detail(self, cmid):
        await self.ensure_login()
        response = await self.get(f"/mod/assign/view.php?id={int(cmid)}")
        doc = soup(response.text)
        main = doc.select_one("#region-main") or doc.select_one('[role="main"]')
        if not main:
            raise VCError("Assignment page unavailable.")
        title = main.select_one("h2")
        intro = main.select_one("#intro")
        links = []
        for a in (intro.select('a[href*="/pluginfile.php/"]') if intro else []):
            url = urljoin(BASE, a["href"])
            if url not in [v["url"] for v in links]:
                links.append({"name": a.get_text(" ", strip=True) or Path(urlparse(url).path).name, "url": url})
        status = main.select_one(".submissionstatustable")
        submission_files = []
        if status:
            for a in status.select('a[href*="/assignsubmission_file/submission_files/"]'):
                url = urljoin(BASE, a["href"])
                if url not in [f["url"] for f in submission_files]:
                    submission_files.append({"name": a.get_text(" ", strip=True) or Path(urlparse(url).path).name,
                                             "url": url})
        state = "unknown"
        if status and status.select_one(".submissionstatussubmitted"):
            state = "submitted"
        elif status and status.select_one(".submissionstatusdraft"):
            state = "draft"
        elif status and status.select_one(".submissionstatusnew"):
            state = "new"
        modified = ""
        if status:
            for row in status.select("tr"):
                label, value = row.select_one("th"), row.select_one("td")
                if label and value and label.get_text(" ", strip=True).casefold() in {"last modified", "آخرین تغییر"}:
                    modified = value.get_text(" ", strip=True)
        remaining = status.select_one(".timeremaining") if status else None
        first_status = status.select_one("tr td") if status else None
        page_title = doc.title.get_text(" ", strip=True) if doc.title else ""
        course = page_title.split(": ", 1)[0] if ": " in page_title else ""
        dates = main.select_one('[data-region="activity-dates"]')
        if intro:
            intro = soup(str(intro))
            for attachments in intro.select('.introattachments, [id^="assign_files_tree"], .fileuploadsubmission, .fileuploadsubmissiontime'):
                attachments.decompose()
        description = intro.get_text("\n", strip=True) if intro else ""
        return {"id": int(cmid), "name": title.get_text(" ", strip=True) if title else str(cmid),
                "description": description or "No description provided.",
                "dates": dates.get_text(" ", strip=True) if dates else "",
                "status": status.get_text(" ", strip=True) if status else "",
                "submission_state": state, "submission_files": submission_files,
                "submission_label": first_status.get_text(" ", strip=True) if first_status else "",
                "course": course,
                "modified": modified, "timing": remaining.get_text(" ", strip=True) if remaining else "",
                "server_date": response.headers.get("date", ""), "checked_at": utc_now(),
                "attachments": links, "url": str(response.url)}

    async def remote_manifest(self, detail):
        """Hash only files in the student's submission, never teacher attachments."""
        result = []
        for file in detail["submission_files"]:
            size, digest = 0, hashlib.sha256()
            async with self.http.stream("GET", self.trusted(file["url"]),
                                        headers={"Cache-Control": "no-cache"}) as response:
                response.raise_for_status()
                if urlparse(str(response.url)).hostname != "vc.kntu.ac.ir" or "/login/" in str(response.url):
                    raise VCError("Submission file download requires authentication; verification is incomplete.")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    digest.update(chunk)
            result.append({"name": file["name"], "size": size, "sha256": digest.hexdigest()})
        return result

    async def verify_submission(self, cmid, expected):
        if not expected:
            raise VCError("No expected file fingerprints are available. Cannot certify this submission.")
        before = await self.detail(cmid)
        actual = await self.remote_manifest(before)
        def keyed(files):
            if len({f["name"] for f in files}) != len(files):
                raise VCError("Ambiguous duplicate submission filenames. Verification is incomplete.")
            return {f["name"]: (f["size"], f["sha256"]) for f in files}
        actual_map, expected_map = keyed(actual), keyed(expected)
        if actual_map != expected_map:
            missing = sorted(expected_map.keys() - actual_map.keys())
            extra = sorted(actual_map.keys() - expected_map.keys())
            changed = sorted(name for name in actual_map.keys() & expected_map.keys()
                             if actual_map[name] != expected_map[name])
            problems = []
            if missing:
                problems.append("missing: " + ", ".join(missing))
            if extra:
                problems.append("unexpected: " + ", ".join(extra))
            if changed:
                problems.append("bytes/SHA-256 differ: " + ", ".join(changed))
            raise VCError("Submission verification failed (" + "; ".join(problems) + "). Turn-in is NOT verified.")
        after = await self.detail(cmid)
        for field in ("submission_files", "submission_state", "modified"):
            if before[field] != after[field]:
                raise VCError("Moodle's submission changed during verification. Turn-in is NOT verified.")
        after.update(verified_files=actual, integrity_verified=True)
        return after

    async def rules(self, cmid):
        await self.ensure_login()
        response = await self.get(f"/mod/assign/view.php?id={int(cmid)}&action=editsubmission")
        doc = soup(response.text)
        form = doc.select_one('form:has(input[name="files_filemanager"])')
        if not form:
            raise VCError("File submission is unavailable (closed, locked, text-only, or no permission).")
        configs = list(js_objects(response.text, "M.form_filemanager.init"))
        if not configs:
            raise VCError("Cannot read upload limits; refusing to guess.")
        config = configs[0]
        options = config.get("options", config)
        picker = config.get("fpoptions", options.get("filepicker", {}))
        if not picker:
            pickers = list(js_objects(response.text, "M.core_filepicker.init"))
            picker = pickers[0] if pickers else {}
        if "maxfiles" not in options or "maxbytes" not in options:
            raise VCError("Missing file count/size limits; refusing to guess.")
        accepted = options.get("accepted_types", picker.get("accepted_types", []))
        if isinstance(accepted, dict):
            accepted = list(accepted.values())
        if isinstance(accepted, str):
            accepted = [accepted]
        if any(e.get("type") == "folder" for e in options.get("list", [])):
            raise VCError("This submission contains folders. Please edit it in Moodle to preserve the full file tree.")
        existing = [e.get("fullname") or e.get("filename") for e in options.get("list", [])
                    if e.get("type") != "folder" and (e.get("fullname") or e.get("filename"))]
        if form.select_one('textarea[name="onlinetext_editor[text]"]'):
            raise VCError("This assignment also uses online text. Please submit it in the browser.")
        checks = {e["name"]: e.get("value", "1") for e in form.select('input[type="checkbox"][name]')}
        declarations = []
        for checkbox in form.select('input[type="checkbox"][name]'):
            label = form.find("label", attrs={"for": checkbox.get("id")})
            if not label:
                raise VCError("Cannot read the submission declaration. Please use Moodle.")
            declarations.append(label.get_text(" ", strip=True))
        return Rules(int(options["maxfiles"]), int(options["maxbytes"]), list(accepted), existing,
                     options, picker, form_data(form), urljoin(str(response.url), form.get("action", "")),
                     "\n".join(declarations), checks)

    async def save(self, cmid, paths, expected_declaration="", expected_files=None, progress=None):
        """Add files, preserving existing files; no automatic final-submit declaration."""
        rules = await self.rules(cmid)
        rules.validate(paths)
        if rules.declaration != expected_declaration:
            raise VCError("The submission declaration changed. Request /review again.")
        if expected_files is None:
            current = await self.detail(cmid)
            expected_files = await self.remote_manifest(current) + file_manifest(paths)
        if progress:
            progress("uploading")
        repositories = rules.picker.get("repositories", {})
        upload = next((r for r in repositories.values() if r.get("type") == "upload"), None)
        if not upload:
            raise VCError("Upload repository unavailable.")
        itemid = rules.fields["files_filemanager"]
        for path in paths:
            started = utc_now()
            clock_started = time.monotonic()
            fields = {"repo_id": str(upload["id"]), "itemid": itemid, "env": "filemanager",
                      "sesskey": self.sesskey, "savepath": "/", "title": path.name,
                      "ctx_id": str(rules.options.get("context", {}).get("id", rules.options.get("contextid", ""))),
                      "license": rules.picker.get("defaultlicense", "unknown"), "author": "",
                      "client_id": rules.options.get("client_id", ""), "accepted_types[]": rules.accepted,
                      "maxbytes": str(rules.max_bytes), "areamaxbytes": str(rules.options.get("areamaxbytes", -1))}
            with path.open("rb") as handle:
                response = await self.http.post(BASE + "/repository/repository_ajax.php", params={"action": "upload"},
                                                data=fields, files={"repo_upload_file": (path.name, handle)}, timeout=180)
            response.raise_for_status()
            result = response.json()
            if result.get("error") or result.get("event") == "fileexists" or not (result.get("url") or result.get("file")):
                raise VCError("Moodle rejected the upload; no assignment changes were saved.")
            if progress:
                progress("uploading", upload={"name": path.name, "started_at": started, "acknowledged_at": utc_now(),
                                               "round_trip_ms": round((time.monotonic() - clock_started) * 1000, 3)})
        fields = dict(rules.fields, submitbutton="Save changes")
        fields.update(rules.checkboxes or {})
        if progress:
            progress("saving", save_requested_at=utc_now())
        response = await self.http.post(self.trusted(rules.action), data=fields)
        response.raise_for_status()
        if progress:
            progress("verifying", save_acknowledged_at=utc_now(), save_response_date=response.headers.get("date", ""))
        doc = soup(response.text)
        if doc.select_one(".error, .alert-danger, .invalid-feedback:not(:empty)"):
            raise VCError("Moodle reported an error while saving. Check the assignment in your browser.")
        if progress:
            progress("verifying")
        return await self.verify_submission(cmid, expected_files)

    async def final_form(self, cmid):
        await self.ensure_login()
        response = await self.get(f"/mod/assign/view.php?id={int(cmid)}&action=submit")
        doc = soup(response.text)
        form = doc.select_one('form:has(input[name="action"][value="submit"])')
        if not form:
            raise VCError("No final-submit confirmation form is available. Check the assignment in Moodle.")
        statement = form.get_text(" ", strip=True)
        checks = [e["name"] for e in form.select('input[type="checkbox"][name]')]
        return {"action": self.trusted(urljoin(str(response.url), form.get("action", ""))),
                "fields": form_data(form), "checks": checks, "statement": statement}

    async def finalize(self, cmid, expected_statement, expected_files=None, progress=None):
        form = await self.final_form(cmid)
        if form["statement"] != expected_statement:
            raise VCError("The submission declaration changed. Request /finalize again to review it.")
        if not expected_files:
            raise VCError("Review the exact files before final submission. Use /finalize again.")
        verified = await self.verify_submission(cmid, expected_files)
        if verified["submission_state"] != "draft":
            raise VCError("Moodle does not show an editable draft. Final submission was not sent.")
        fields = dict(form["fields"], submitbutton="Continue")
        for name in form["checks"]:
            fields[name] = "1"
        if progress:
            progress("finalizing", submit_requested_at=utc_now())
        response = await self.http.post(form["action"], data=fields)
        response.raise_for_status()
        if progress:
            progress("verifying", submit_acknowledged_at=utc_now(), submit_response_date=response.headers.get("date", ""))
        detail = await self.verify_submission(cmid, expected_files)
        if detail["submission_state"] != "submitted":
            raise VCError("Files are intact, but Moodle does NOT confirm final submission. Use /verify before retrying.")
        return detail


def settings():
    import os
    values = dict(dotenv_values("New Text Document.txt"))
    values.update({k: v for k, v in dotenv_values(".env").items() if v})
    values.update(os.environ)
    username = values.get("KNTU_USERNAME") or values.get("User")
    password = values.get("KNTU_PASSWORD") or values.get("Password")
    if not username or not password:
        raise VCError("Set KNTU_USERNAME and KNTU_PASSWORD in .env.")
    return username, password, values


async def check(cmid):
    username, password, _ = settings()
    client = VCClient(username, password)
    try:
        await client.login()
        print("University login: OK")
        events = await client.assignments()
        print(f"Timeline: {len(events)} assignment(s)")
        for e in events:
            print(e["id"], e["name"], e["due"])
        detail = await client.detail(cmid)
        print("Assignment:", detail["name"])
        print("Attachments:", len(detail["attachments"]))
        print("Limits:", (await client.rules(cmid)).summary())
    finally:
        await client.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Read-only university login/calendar/limits check")
    parser.add_argument("--check", type=int, required=True)
    asyncio.run(check(parser.parse_args().check))

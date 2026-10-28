"""Telegram-native presentation. Escape all remote/user content before HTML rendering."""
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

TZ = ZoneInfo("Asia/Tehran")
HOME = "🏠 Home"
ASSIGNMENTS = "📚 Assignments"
UPLOAD = "📎 My upload"
HELP = "❓ Help"


def e(value):
    return escape(str(value), quote=True)


def html_chunks(text, limit=3500):
    """Split escaped HTML without losing text, breaking entities, or unbalancing tags."""
    class Splitter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.parts, self.current = [], [], ""

        def closing(self):
            return "".join(f"</{tag}>" for tag in reversed(self.stack))

        def flush(self):
            self.parts.append(self.current + self.closing())
            self.current = "".join(f"<{tag}>" for tag in self.stack)

        def handle_starttag(self, tag, attrs):
            if tag not in {"b", "i", "code"} or attrs:
                raise ValueError("Unsupported locally authored HTML")
            if len(self.current) + len(self.closing()) + 2 * len(tag) + 5 > limit:
                self.flush()
            self.stack.append(tag)
            self.current += f"<{tag}>"

        def handle_endtag(self, tag):
            if not self.stack or self.stack[-1] != tag:
                raise ValueError("Unbalanced locally authored HTML")
            self.stack.pop()
            self.current += f"</{tag}>"

        def handle_data(self, data):
            for char in data:
                encoded = e(char)
                if len(self.current) + len(self.closing()) + len(encoded) > limit:
                    self.flush()
                self.current += encoded
    parser = Splitter()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ValueError("Unclosed locally authored HTML")
    if parser.current:
        parser.parts.append(parser.current)
    return parser.parts or [""]


def button(text, data=None, style=None, url=None):
    return Button(text, callback_data=data, style=style, url=url)


def markup(*rows):
    return InlineKeyboardMarkup(rows)


def keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton(ASSIGNMENTS, style="primary"), UPLOAD], [HOME, HELP]],
        resize_keyboard=True, is_persistent=True, input_field_placeholder="Choose an action or attach a document…")


def size(value):
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KiB"
    return f"{value / 1024**2:.1f} MiB"


def file_list(files, hashes=False):
    return "\n".join(f"📄 <b>{e(f['name'])}</b> · {size(f['size'])}"
                     + (f"\n<code>{e(f['sha256'])}</code>" if hashes else "") for f in files)


def rules_text(rules):
    count = str(rules.max_files) if rules.max_files >= 0 else "unlimited"
    limit = size(rules.max_bytes) if rules.max_bytes > 0 else "site limit"
    formats = ", ".join(rules.accepted) if rules.accepted and rules.accepted != ["*"] else "Any format"
    total = int(rules.options.get("areamaxbytes", -1))
    text = f"<b>Upload limits</b>\n{e(count)} files · {e(limit)} each\nFormats: {e(formats)}\nExisting Moodle files: {len(rules.existing)}"
    if total > 0:
        text += f"\nTotal Moodle storage: {size(total)}"
    return text + "\nTelegram transfer limit: 20 MiB per document"


def assignment_list(events, kind, page, now):
    filtered = [x for x in events if (x["due"] < now) == (kind == "overdue")]
    filtered.sort(key=lambda x: x["due"], reverse=kind == "overdue")
    count = len(filtered)
    page = max(0, min(page, max(0, (count - 1) // 5)))
    title = "Overdue" if kind == "overdue" else "Upcoming"
    lines = [f"📚 <b>{title} assignments</b>", f"{count} in your Moodle timeline · Tehran time"]
    rows = [[button(("✓ " if kind == "upcoming" else "") + "Upcoming", "list:upcoming:0", "primary" if kind == "upcoming" else None),
             button(("✓ " if kind == "overdue" else "") + "Overdue", "list:overdue:0", "primary" if kind == "overdue" else None)]]
    for index, item in enumerate(filtered[page * 5:page * 5 + 5], start=page * 5 + 1):
        date = datetime.fromtimestamp(item["due"], TZ).strftime("%a, %d %b · %H:%M")
        hours = abs(item["due"] - now) / 3600
        relative = f"{max(1, round(hours))}h" if hours < 48 else f"{hours / 24:.0f}d"
        relative = relative + " overdue" if kind == "overdue" else "in " + relative
        lines.append(f"\n<b>{index}. {e(item['name'])}</b>\n{e(item['course'])}\n{'🔴' if kind == 'overdue' else '🗓'} {date} · {relative}")
        label = item["name"]
        rows.append([button(f"{index}. {label[:52]}", f"open:{item['id']}")])
    if not count:
        lines.append(f"\nNo {title.lower()} assignments appear here.")
    if count > 5:
        nav = []
        if page:
            nav.append(button("‹ Previous", f"list:{kind}:{page - 1}"))
        if (page + 1) * 5 < count:
            nav.append(button("Next ›", f"list:{kind}:{page + 1}"))
        rows.append(nav)
        lines.append(f"\nPage {page + 1} of {(count + 4) // 5}")
    lines.append("\n<i>Completed work and assignments without due dates may be absent.</i>")
    rows.append([button("↻ Refresh", f"list:{kind}:{page}"), button("⌂ Home", "home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def receipt_card(receipt):
    state, cmid = receipt["state"], receipt["cmid"]
    verified = receipt.get("integrity_verified")
    if state == "submitted" and verified:
        heading = "✅ Turned in · verified"
        body = "Moodle reports <b>submitted</b>. The downloaded submission files match the reviewed bytes and SHA-256 hashes."
    elif state == "draft" and verified:
        heading = "🟠 Saved as draft · not turned in"
        body = "Your files match, but Moodle still reports a draft. Complete the separate final submission below."
    else:
        heading = "⚠️ Turn-in is not verified"
        body = "The outcome is uncertain. Recheck safely before trying another upload."
    lines = [f"<b>{heading}</b>", body, f"<b>Assignment {cmid}</b>", file_list(receipt["expected"])]
    if receipt.get("verified_at"):
        date = datetime.fromisoformat(receipt["verified_at"]).astimezone(TZ).strftime("%d %b %Y · %H:%M:%S")
        lines.append(f"Last content check: {date} Tehran")
    if receipt.get("server_timing"):
        lines.append(f"Moodle timing: {e(receipt['server_timing'])}")
    if receipt.get("verification_issue"):
        lines.append(f"<b>Check needed</b>\n{e(receipt['verification_issue'])}")
    rows = []
    if state == "draft" and verified:
        rows.append([button("Review final submission →", f"finish:{cmid}", "success")])
    rows.extend([[button("↻ Verify again", f"check:{cmid}", "primary"), button("Receipt details", f"receipt:{cmid}")],
                 [button("Open Moodle ↗", url=f"https://vc.kntu.ac.ir/mod/assign/view.php?id={cmid}"), button("⌂ Home", "home")]])
    return "\n\n".join(lines), InlineKeyboardMarkup(rows)

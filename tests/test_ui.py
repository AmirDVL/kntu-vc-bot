from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import bot
import ui
from vc_client import VCError


def update(data="home", uid=7):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(type="private"),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()),
                           callback_query=SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                                                          edit_message_reply_markup=AsyncMock()))


def context(tmp_path):
    from submission import ReceiptStore
    return SimpleNamespace(args=None, application=SimpleNamespace(bot_data={
        "admin_id": 1, "receipts": ReceiptStore(tmp_path / "receipts.sqlite3")}))


def test_native_colors_and_persistent_menu():
    assert ui.button("Save", "save:token", "success").to_dict()["style"] == "success"
    assert ui.button("Discard", "discard:token", "danger").to_dict()["style"] == "danger"
    assert ui.button("Browse", "browse", "primary").to_dict()["style"] == "primary"
    assert ui.keyboard().is_persistent
    assert ui.keyboard().resize_keyboard


def test_long_html_is_lossless_escaped_and_balanced():
    original = ('<script>& "فارسی" 😀\n' * 1000)
    chunks = ui.html_chunks("<b>" + ui.e(original) + "</b>")
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tags, self.text = [], ""
        def handle_starttag(self, tag, attrs):
            assert tag == "b" and not attrs
            self.tags.append(tag)
        def handle_endtag(self, tag):
            assert self.tags.pop() == tag
        def handle_data(self, data):
            self.text += data
    recovered = ""
    for chunk in chunks:
        assert len(chunk) <= 3500
        parser = Parser()
        parser.feed(chunk)
        parser.close()
        assert not parser.tags
        recovered += parser.text
    assert recovered == original


def test_assignment_filter_pagination_and_escaping():
    events = [{"id": i, "name": f"<Homework {i}>", "course": "<unsafe>&", "due": 1000 + i} for i in range(12)]
    events.append({"id": 99, "name": "Past", "course": "Old", "due": 1})
    text, keys = ui.assignment_list(events, "upcoming", 1, now=100)
    assert "Page 2 of 3" in text
    assert "<unsafe>" not in text
    assert "&lt;unsafe&gt;&amp;" in text
    assert len([b for row in keys.inline_keyboard for b in row if b.callback_data.startswith("open:")]) == 5
    assert "Past" not in text
    text, _ = ui.assignment_list(events, "overdue", 10000, now=100)
    assert "Past" in text and "Homework" not in text


@pytest.mark.parametrize("state,verified,expected", [
    ("submitted", True, "Turned in · verified"),
    ("draft", True, "not turned in"),
    ("unknown", True, "Turn-in is not verified"),
    ("submitted", False, "Turn-in is not verified")])
def test_compact_receipt_keeps_verdict_and_hides_hashes(state, verified, expected):
    receipt = {"cmid": 42, "state": state, "integrity_verified": verified,
               "expected": [{"name": "<answer>.pdf", "size": 20, "sha256": "a" * 64}]}
    text, keys = ui.receipt_card(receipt)
    assert expected in text
    assert "a" * 64 not in text
    assert "&lt;answer&gt;" in text
    assert any(b.callback_data == "receipt:42" for row in keys.inline_keyboard for b in row)
    if not verified or state != "draft":
        assert not any(b.callback_data == "finish:42" for row in keys.inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_navigation_restores_command_args_and_never_saves(tmp_path):
    c = context(tmp_path)
    original = c.args
    with patch.object(bot, "assignment", new_callable=AsyncMock) as assignment:
        await bot.navigate(update("open:42"), c)
        assignment.assert_awaited_once()
    assert c.args is original


@pytest.mark.asyncio
async def test_admin_shortcut_is_not_available_to_other_users(tmp_path):
    c, u = context(tmp_path), update("login", uid=9)
    with patch.object(bot, "establish", new_callable=AsyncMock) as establish:
        await bot.navigate(u, c)
        establish.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_file_invalidates_confirmation_and_stale_buttons(tmp_path):
    uid = 789
    s = bot.UserSession(SimpleNamespace())
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"homework")
    s.cmid, s.files, s.upload_nonce = 42, [path], "oldnonce"
    s.confirm, s.confirm_action, s.reviewed = "oldtoken", "save", {"staged": []}
    bot.sessions[uid] = s
    try:
        with patch.object(bot, "staging", new_callable=AsyncMock):
            await bot.navigate(update("remove:oldnonce:0", uid), context(tmp_path))
        assert s.files == [] and not path.exists()
        assert s.confirm is None and s.reviewed is None
        with pytest.raises(VCError, match="changed"):
            await bot.navigate(update("remove:oldnonce:0", uid), context(tmp_path))
    finally:
        bot.sessions.pop(uid)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["discard:oldnonce", "switch:oldnonce:42"])
async def test_old_discard_and_switch_buttons_cannot_destroy_new_staged_files(tmp_path, data):
    uid = 789
    s = bot.UserSession(SimpleNamespace())
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"homework")
    s.cmid, s.files, s.upload_nonce = 42, [path], "newnonce"
    bot.sessions[uid] = s
    try:
        with pytest.raises(VCError, match="changed"):
            await bot.navigate(update(data, uid), context(tmp_path))
        assert path.exists()
        assert s.files == [path]
    finally:
        bot.sessions.pop(uid)


@pytest.mark.asyncio
async def test_switch_requires_explicit_confirmation_before_clearing_files(tmp_path):
    uid = 789
    s = bot.UserSession(SimpleNamespace())
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"homework")
    s.cmid, s.files, s.upload_nonce = 42, [path], "nonce"
    bot.sessions[uid] = s
    c = context(tmp_path)
    c.args = ["43"]
    try:
        await bot.upload(update("upload:43", uid), c)
        assert path.exists() and s.cmid == 42
    finally:
        bot.sessions.pop(uid)


@pytest.mark.asyncio
async def test_full_button_review_save_receipt_flow(tmp_path):
    """Exercise UI routing/confirmation; all Moodle operations are mocked."""
    from vc_client import Rules
    uid = 789
    client = SimpleNamespace(rules=AsyncMock(return_value=Rules(2, 1024, [".pdf"], [], {}, {}, {}, "https://vc.kntu.ac.ir")))
    s = bot.UserSession(client)
    path = tmp_path / "homework.pdf"
    path.write_bytes(b"answer")
    s.cmid, s.files, s.upload_name, s.upload_nonce = 42, [path], "Course · Assignment", "nonce"
    bot.sessions[uid] = s
    receipt = {"cmid": 42, "state": "draft", "integrity_verified": True,
               "expected": [{"name": path.name, "size": 6, "sha256": "a" * 64}]}
    service = SimpleNamespace(prepare=AsyncMock(return_value={"staged": receipt["expected"]}), save=AsyncMock(return_value=receipt))
    c = context(tmp_path)
    try:
        with patch.object(bot, "submission_service", return_value=service):
            u = update("review", uid)
            await bot.navigate(u, c)
            review_message = u.callback_query.edit_message_text.call_args
            buttons = review_message.kwargs["reply_markup"].inline_keyboard
            save_button = buttons[0][0]
            assert save_button.style == "success"
            assert "Course · Assignment" in review_message.args[0]
            service.save.assert_not_awaited()
            u = update(save_button.callback_data, uid)
            await bot.callback(u, c)
            service.save.assert_awaited_once()
            assert s.files == [] and s.cmid is None
            responses = [call.args[0] for call in u.effective_message.reply_text.call_args_list]
            assert any("not turned in" in text for text in responses)
    finally:
        bot.sessions.pop(uid)

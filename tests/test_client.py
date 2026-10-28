import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from vc_client import Rules, VCClient, VCError, js_objects, file_manifest
from bot import UserSession, safe_name


def rules(**overrides):
    args = dict(max_files=2, max_bytes=10, accepted=[".pdf"], existing=[],
                options={}, picker={}, fields={"files_filemanager": "123"},
                action="https://vc.kntu.ac.ir/mod/assign/view.php")
    args.update(overrides)
    return Rules(**args)


def test_format_size_count_and_existing_files(tmp_path):
    pdf = tmp_path / "homework.PDF"
    pdf.write_bytes(b"%PDF-test")
    rules().validate([pdf])
    with pytest.raises(VCError, match="Maximum"):
        rules(existing=["a.pdf", "b.pdf"]).validate([pdf])
    with pytest.raises(VCError, match="already exists"):
        rules(existing=[pdf.name]).validate([pdf])
    with pytest.raises(VCError, match="exceeds"):
        rules(max_bytes=1).validate([pdf])
    with pytest.raises(VCError, match="allowed formats"):
        rules(accepted=[".zip"]).validate([pdf])
    with pytest.raises(VCError, match="storage limit"):
        rules(options={"areamaxbytes": 10, "list": [{"size": 9}]}).validate([pdf])


def test_mime_rules_and_wildcard(tmp_path):
    file = tmp_path / "hw.pdf"
    file.write_bytes(b"pdf")
    rules(accepted=["application/pdf"]).validate([file])
    rules(accepted=["application/*"]).validate([file])
    rules(accepted=["*"]).validate([file])
    with pytest.raises(VCError):
        rules(accepted=["image/*"]).validate([file])


def test_json_config_is_parsed_not_executed():
    html = 'M.form_filemanager.init(Y, {"options":{"maxfiles":20,"accepted_types":["*"]}});'
    assert list(js_objects(html, "M.form_filemanager.init"))[0]["options"]["maxfiles"] == 20


def test_safe_filenames():
    assert "/" not in safe_name("../../homework.pdf")
    assert "\\" not in safe_name("..\\homework.pdf")
    assert safe_name("CON.pdf") == "_CON.pdf"
    with pytest.raises(VCError):
        safe_name("..")


def test_sessions_do_not_share_staged_files(tmp_path):
    a = UserSession(None)
    b = UserSession(None)
    a.folder = tmp_path / "a"
    a.folder.mkdir()
    p = a.folder / "hw.pdf"
    p.write_bytes(b"test")
    a.files.append(p)
    a.confirm = "old"
    assert b.files == []
    a.clear_upload()
    assert not p.exists()
    assert a.confirm is None


@pytest.mark.asyncio
async def test_calendar_pagination_and_deduplication():
    client = VCClient("test", "test")
    client.ensure_login = AsyncMock()
    def event(i):
        return {"id": i, "modulename": "assign", "eventtype": "due", "url": f"https://vc.kntu.ac.ir/mod/assign/view.php?id={i}", "name": str(i), "timestart": i}
    client.ajax = AsyncMock(side_effect=[{"events": [event(i) for i in range(1, 51)], "lastid": 50},
                                        {"events": [event(50), event(51)], "lastid": 51}])
    try:
        results = await client.assignments()
        assert len(results) == 51
        assert client.ajax.call_args_list[1].args[1]["aftereventid"] == 50
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_revalidates_before_upload_and_verifies(tmp_path):
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"pdf")
    requests = []
    saved = False
    def handler(request):
        nonlocal saved
        requests.append(request)
        if "repository_ajax" in str(request.url):
            return httpx.Response(200, json={"url": "https://vc.kntu.ac.ir/draftfile.php/file", "file": "hw.pdf"})
        if request.method == "POST":
            saved = True
            return httpx.Response(200, text="Saved")
        if "pluginfile.php" in str(request.url):
            return httpx.Response(200, content=b"pdf")
        files = '<a href="/pluginfile.php/1/assignsubmission_file/submission_files/1/hw.pdf">hw.pdf</a>' if saved else ""
        return httpx.Response(200, text=f'<section id="region-main"><h2>Homework</h2><div class="submissionstatustable"><td class="submissionstatusdraft">Draft</td>{files}</div></section>')
    client = VCClient("test", "test")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = rules(picker={"repositories": {"5": {"id": "5", "type": "upload"}}})
    client.rules = AsyncMock(return_value=r)
    client.ensure_login = AsyncMock()
    try:
        result = await client.save(1, [path])
        assert result["integrity_verified"]
        posts = [r for r in requests if r.method == "POST"]
        assert len(posts) == 2
        assert b'files_filemanager=123' in posts[1].content
        r.max_files = 0
        requests.clear()
        with pytest.raises(VCError):
            await client.save(1, [path], expected_files=file_manifest([path]))
        assert requests == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_failure_does_not_save_form(tmp_path):
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"pdf")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"error": "size limit"})
    client = VCClient("test", "test")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.rules = AsyncMock(return_value=rules(picker={"repositories": {"5": {"id": "5", "type": "upload"}}}))
    try:
        with pytest.raises(VCError, match="rejected"):
            await client.save(1, [path], expected_files=file_manifest([path]))
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_finalization_requires_unchanged_statement():
    client = VCClient("test", "test")
    client.final_form = AsyncMock(return_value={"statement": "new statement"})
    try:
        with pytest.raises(VCError, match="changed"):
            await client.finalize(1, "old statement")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_requires_review_of_current_declaration(tmp_path):
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"pdf")
    client = VCClient("test", "test")
    client.rules = AsyncMock(return_value=rules(declaration="I certify this is my work."))
    try:
        with pytest.raises(VCError, match="declaration changed"):
            await client.save(1, [path])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_other_users_cannot_use_admin_credentials():
    from types import SimpleNamespace
    from unittest.mock import patch
    import bot
    update = SimpleNamespace(effective_user=SimpleNamespace(id=2),
                             effective_chat=SimpleNamespace(type="private"))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"admin_id": 1}))
    with patch.object(bot, "establish", new_callable=AsyncMock) as establish, patch.object(bot, "reply", new_callable=AsyncMock):
        await bot.login(update, context)
        establish.assert_not_called()


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_save():
    from types import SimpleNamespace
    import bot
    uid = 123456
    client = SimpleNamespace(save=AsyncMock())
    s = UserSession(client)
    s.confirm = "current"
    bot.sessions[uid] = s
    update = SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                             effective_chat=SimpleNamespace(type="private"),
                             callback_query=SimpleNamespace(answer=AsyncMock(), data="save:old"))
    try:
        with pytest.raises(VCError, match="expired"):
            await bot.callback(update, None)
        client.save.assert_not_called()
    finally:
        bot.sessions.pop(uid)

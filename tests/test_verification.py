import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import receipt_text
from submission import ReceiptStore, SubmissionService
from vc_client import VCClient, VCError, file_manifest


def manifest(data=b"answer", name="hw.pdf"):
    return [{"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]


def detail(state="submitted"):
    return {"id": 1, "submission_state": state, "submission_files": [
        {"name": "hw.pdf", "url": "https://vc.kntu.ac.ir/pluginfile.php/1/assignsubmission_file/submission_files/2/hw.pdf"}],
        "modified": "Moodle's timestamp", "checked_at": "2026-10-28T10:00:00.123+00:00",
        "server_date": "Wed, 28 Oct 2026 10:00:00 GMT", "timing": "Submitted early"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["draft", "submitted"])
async def test_exact_content_checked_independently_of_turnin_state(state):
    client = VCClient("user", "secret")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"answer")))
    client.detail = AsyncMock(return_value=detail(state))
    try:
        result = await client.verify_submission(1, manifest())
        assert result["integrity_verified"]
        assert result["submission_state"] == state
        assert client.detail.await_count == 2  # Fresh state before and after download.
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_same_filename_and_size_but_wrong_bytes_is_failure():
    client = VCClient("user", "secret")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"WRONG!")))
    client.detail = AsyncMock(return_value=detail())
    try:
        with pytest.raises(VCError, match="SHA-256 differ: hw.pdf"):
            await client.verify_submission(1, manifest())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_teacher_attachment_cannot_count_as_student_submission():
    html = '''<section id="region-main"><h2>Homework</h2><div id="intro">
    <a href="/pluginfile.php/1/mod_assign/introattachment/0/hw.pdf">hw.pdf</a></div>
    <div class="submissionstatustable"><td class="submissionstatusdraft">Draft</td></div></section>'''
    client = VCClient("user", "secret")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html)))
    client.ensure_login = AsyncMock()
    try:
        d = await client.detail(1)
        assert len(d["attachments"]) == 1
        assert d["submission_files"] == []
        with pytest.raises(VCError, match="missing: hw.pdf"):
            await client.verify_submission(1, manifest())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_change_during_hash_download_invalidates_verification():
    client = VCClient("user", "secret")
    client.detail = AsyncMock(side_effect=[detail("submitted"), detail("draft")])
    client.remote_manifest = AsyncMock(return_value=manifest())
    try:
        with pytest.raises(VCError, match="changed during verification"):
            await client.verify_submission(1, manifest())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_extra_file_is_not_silently_accepted():
    client = VCClient("user", "secret")
    client.detail = AsyncMock(return_value=detail())
    client.remote_manifest = AsyncMock(return_value=manifest() + manifest(b"other", "other.pdf"))
    try:
        with pytest.raises(VCError, match="unexpected: other.pdf"):
            await client.verify_submission(1, manifest())
    finally:
        await client.close()


def make_service(tmp_path, owner=1, username="user"):
    client = SimpleNamespace(username=username, account_id=1 if username == "user" else 2,
                             verify_submission=AsyncMock(return_value=detail()),
                             detail=AsyncMock(return_value=detail()), remote_manifest=AsyncMock(return_value=[]))
    return SubmissionService(client, ReceiptStore(tmp_path / "receipts.sqlite3"), owner)


def test_receipt_store_closes_database_handles(tmp_path):
    path = tmp_path / "receipts.sqlite3"
    store = ReceiptStore(path)
    receipt = {"id": "test-receipt", "cmid": 1, "state": "unknown"}
    store.put(1, "account", receipt)
    assert store.latest(1, "account", 1) == receipt
    # Windows refuses this if a connection still holds the database open.
    path.unlink()
    assert not path.exists()


@pytest.mark.asyncio
async def test_save_timeout_recovers_by_readback_without_replaying_post(tmp_path):
    service = make_service(tmp_path)
    performed = 0
    async def perform(progress):
        nonlocal performed
        performed += 1
        progress("saving", save_requested_at="2026-10-28T09:59:00+00:00")
        # Server accepted the save, but the HTTP acknowledgement was lost.
        raise httpx.ReadTimeout("timeout")
    receipt = await service._run(1, manifest(), "save", perform)
    assert performed == 1
    assert receipt["state"] == "submitted"
    assert receipt["integrity_verified"]
    assert "TURNED IN — VERIFIED" in receipt_text(receipt)


@pytest.mark.asyncio
async def test_unresolved_write_is_durable_and_blocks_duplicate_upload(tmp_path, monkeypatch):
    monkeypatch.setattr("submission.asyncio.sleep", AsyncMock())
    service = make_service(tmp_path)
    service.client.verify_submission.side_effect = VCError("Submission verification failed: missing hw.pdf")
    async def perform(progress):
        progress("saving")
        raise httpx.ReadTimeout("timeout")
    receipt = await service._run(1, manifest(), "save", perform)
    assert receipt["state"] == "unknown"
    assert service.client.verify_submission.await_count == 3
    assert "TURN-IN IS NOT VERIFIED" in receipt_text(receipt)
    restarted = make_service(tmp_path)
    with pytest.raises(VCError, match="uncertain outcome"):
        restarted.check_pending(1)
    recovered = await restarted.verify(1)
    assert recovered["state"] == "submitted"
    restarted.check_pending(1)


@pytest.mark.asyncio
async def test_receipts_are_scoped_to_both_telegram_user_and_university_account(tmp_path):
    service = make_service(tmp_path)
    async def perform(progress):
        return detail()
    receipt = await service._run(1, manifest(), "save", perform)
    assert service.latest(1)["id"] == receipt["id"]
    assert make_service(tmp_path, owner=2).latest(1) is None
    assert make_service(tmp_path, username="other_account").latest(1) is None


@pytest.mark.asyncio
async def test_final_submit_timeout_with_draft_is_not_reported_as_success(tmp_path):
    service = make_service(tmp_path)
    service.client.verify_submission.return_value = detail("draft")
    async def perform(progress):
        progress("finalizing")
        raise httpx.ReadTimeout("timeout")
    receipt = await service._run(1, manifest(), "finalize", perform)
    assert receipt["state"] == "unknown"
    assert receipt["integrity_verified"]
    assert "TURN-IN IS NOT VERIFIED" in receipt_text(receipt)
    assert "TURNED IN — VERIFIED" not in receipt_text(receipt)
    assert (await service.verify(1))["state"] == "unknown"


@pytest.mark.asyncio
async def test_draft_receipt_cannot_claim_turnin(tmp_path):
    service = make_service(tmp_path)
    async def perform(progress):
        return detail("draft")
    receipt = await service._run(1, manifest(), "save", perform)
    assert receipt["state"] == "draft"
    assert "NOT TURNED IN" in receipt_text(receipt)
    assert "TURNED IN — VERIFIED" not in receipt_text(receipt)


@pytest.mark.asyncio
async def test_reviewed_file_mutation_prevents_any_write(tmp_path):
    path = tmp_path / "hw.pdf"
    path.write_bytes(b"original")
    reviewed = {"staged": file_manifest([path]), "baseline": [], "expected": file_manifest([path])}
    path.write_bytes(b"changed")
    service = make_service(tmp_path)
    service.client.save = AsyncMock()
    with pytest.raises(VCError, match="changed since review"):
        await service.save(1, [path], "", reviewed)
    service.client.save.assert_not_called()


@pytest.mark.asyncio
async def test_successful_post_response_cannot_override_fresh_draft_state():
    client = VCClient("user", "secret")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='<td class="submissionstatussubmitted">Submitted</td>')))
    client.final_form = AsyncMock(return_value={"statement": "I agree", "fields": {}, "checks": [],
                                               "action": "https://vc.kntu.ac.ir/mod/assign/view.php"})
    client.verify_submission = AsyncMock(return_value=detail("draft"))
    try:
        with pytest.raises(VCError, match="does NOT confirm final submission"):
            await client.finalize(1, "I agree", manifest())
        assert client.verify_submission.await_count == 2
    finally:
        await client.close()

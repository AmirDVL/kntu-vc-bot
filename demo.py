"""Offline failure demo: no credentials, browser, Telegram, or network access."""
import asyncio
import json
import tempfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from submission import ReceiptStore, SubmissionService
from vc_client import VCClient


class SimulatedMoodle:
    """Small HTTP simulation exercising the real client and receipt service."""
    def __init__(self, mode):
        self.mode = mode
        self.state = "new"
        self.draft = b""
        self.files = {}
        self.save_posts = 0

    def __call__(self, request):
        if request.url.host != "vc.kntu.ac.ir":
            raise AssertionError("Unexpected host: offline simulation only")
        if request.url.path == "/my/index.php":
            return httpx.Response(200, text="Authenticated demo dashboard")
        if request.url.path.startswith("/pluginfile.php/"):
            return httpx.Response(200, content=self.files["answer.txt"])
        if request.url.path == "/repository/repository_ajax.php":
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content)
            self.draft = next(part.get_payload(decode=True) for part in message.iter_parts()
                              if part.get_param("name", header="content-disposition") == "repo_upload_file")
            return httpx.Response(200, json={"file": "answer.txt", "url": "https://vc.kntu.ac.ir/draftfile.php/answer.txt"})
        if request.url.path != "/mod/assign/view.php":
            raise AssertionError("Unexpected offline endpoint")
        if request.method == "POST":
            self.save_posts += 1
            if self.mode == "missing":
                raise httpx.ReadTimeout("Simulated save timeout", request=request)
            self.files["answer.txt"] = self.draft if self.mode != "corrupt" else b"X" * len(self.draft)
            self.state = "draft" if self.mode == "draft" else "submitted"
            if self.mode == "lost-ack":
                raise httpx.ReadTimeout("Server committed; acknowledgement lost", request=request)
            return httpx.Response(200, text="Save acknowledged")
        if parse_qs(request.url.query.decode()).get("action") == ["editsubmission"]:
            config = {"options": {"maxfiles": 2, "maxbytes": 1024, "accepted_types": [".txt"], "list": [],
                                  "filepicker": {"repositories": {"5": {"id": "5", "type": "upload"}}}}}
            return httpx.Response(200, text=f'''<form action="/mod/assign/view.php?id=123">
                <input name="files_filemanager" value="42"><input name="id" value="123">
                <input name="action" value="savesubmission"></form>
                <script>M.form_filemanager.init(Y, {json.dumps(config)});</script>''')
        links = "".join(f'<a href="/pluginfile.php/1/assignsubmission_file/submission_files/2/{name}">{name}</a>'
                        for name in self.files)
        return httpx.Response(200, text=f'''<section id="region-main"><h2>Synthetic assignment</h2>
            <div class="submissionstatustable"><table><tr><th>Submission status</th>
            <td class="submissionstatus{self.state}">{self.state}</td></tr>
            <tr><th>Last modified</th><td>Demo server timestamp</td></tr></table>{links}</div></section>''')


async def scenario(mode):
    server = SimulatedMoodle(mode)
    client = VCClient("offline-demo", "not-a-real-password")
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client.sesskey, client.account_id = "synthetic-session", 7
    try:
        with tempfile.TemporaryDirectory(prefix="vc-offline-demo-") as directory:
            path = Path(directory) / "answer.txt"
            path.write_bytes(b"Synthetic homework, with no personal data.\n")
            store = ReceiptStore(Path(directory) / "receipts.sqlite3")
            service = SubmissionService(client, store, owner=42)
            reviewed = await service.prepare(123, [path])
            receipt = await service.save(123, [path], "", reviewed)
            expected_state = {"lost-ack": "submitted", "draft": "draft", "corrupt": "unknown", "missing": "unknown"}[mode]
            assert receipt["state"] == expected_state
            assert server.save_posts == 1, "A mutation must not be automatically replayed"
            assert SubmissionService(client, ReceiptStore(store.path), owner=42).latest(123)["id"] == receipt["id"]
            print(f"{mode:9} -> {receipt['state']:9} | save POSTs: {server.save_posts} | receipt survived reload")
            if receipt.get("verification_issue"):
                print("             " + receipt["verification_issue"])
    finally:
        await client.close()


async def main():
    print("OFFLINE DEMO: synthetic files and Moodle responses; no external requests.\n")
    for mode in ("lost-ack", "draft", "corrupt", "missing"):
        await scenario(mode)
    print("\nAll scenarios passed. A filename/HTTP acknowledgement alone never certifies turn-in.")


if __name__ == "__main__":
    asyncio.run(main())

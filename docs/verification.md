# Verification evidence

## Reproduce without accounts

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python demo.py
```

Tests and the demo need no `.env`, Telegram token or university account. The demo installs an `httpx.MockTransport`; it cannot reach the real university. It exercises the production Moodle client, hashing, submission service and SQLite receipts with a synthetic document.

The demo covers four distinct server behaviors:

| Scenario | Simulated behavior | Required result |
| --- | --- | --- |
| `lost-ack` | Save commits, then the response times out | Read back matching bytes and submitted status; one save POST |
| `draft` | Save succeeds but final submission is still pending | Report draft, never turned in |
| `corrupt` | Server stores different bytes of the same length | Unknown outcome with a SHA-256 mismatch |
| `missing` | Save times out and the expected file is absent | Unknown outcome; no automatic replay |

The receipt is reloaded from SQLite in every scenario. The simulated server counts save POSTs and asserts that the count remains one.

## Regression tests

See [`tests/test_client.py`](../tests/test_client.py) and [`tests/test_verification.py`](../tests/test_verification.py). They cover:

- File extensions/MIME types, per-file sizes, aggregate storage and count limits.
- Existing-file preservation and duplicate-name rejection.
- Safe local filenames and independent staging lists.
- Calendar pagination and duplicate-event handling.
- Admin credential access control and expired confirmations.
- Real upload/save request construction against a mocked HTTP transport.
- Same filename and same size with different content.
- Teacher attachment versus actual student submission.
- Extra files and state changes during download-back verification.
- Lost save acknowledgement and bounded read-only recovery.
- Durable unresolved-write blocking and receipt isolation across users/accounts.
- Draft status after an ambiguous final-submit response.
- Changed file bytes after user review.
- A successful POST response contradicted by a fresh draft status.
- Native button styles, filtered/paginated assignment lists, safe lossless HTML splitting, callback routing, stale file-removal/discard protection and the button-driven review/save/receipt flow.

The CI matrix runs tests and the offline demo on Windows and Linux, with Python 3.11 and 3.14. CI uses no live-service secrets.

## What this evidence does not prove

Mocked HTTP responses demonstrate application decisions under specified failures. They do not prove that every real Moodle version emits the assumed forms, repository responses, submission classes or file links.

Read-only live checks have covered the university SSO flow, cross-course action calendar, descriptions/attachment discovery, PDF retrieval and file-manager configuration parsing. A real homework save and final submission have **not** been used as integration tests.

Before treating the bot as dependable for deadlines, exercise a disposable assignment with its owner's permission. Record the Moodle version, configuration and observed results for:

1. Save a known file; inspect the browser and compare downloaded content.
2. Confirm the difference between draft and submitted workflows.
3. Exercise format/count/size constraints and existing-file preservation.
4. Disconnect around save/final-submit boundaries, then run `/verify` after reconnecting.
5. Check server-reported timing near a controlled test deadline.

Use synthetic homework and redact all user IDs, cookies, tokens, real course material and private portal screenshots from any published evidence. Adoption counts, uptime, production scale and exactly-once submission are not claimed by this repository.

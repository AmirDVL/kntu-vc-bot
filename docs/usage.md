# KNTU homework Telegram bot

A multi-user bot for `vc.kntu.ac.ir`. Each Telegram user signs in to **their own** university account. The admin ID only permits using the local university credentials with `/login`; it does not restrict who can use the bot.

## Run

Python 3.11 or newer:

```powershell
python -m pip install -r requirements.txt
python -X utf8 bot.py
```

Configure `.env` from `.env.example`. The existing names `User`, `Password`, `Token`, and `AdminID` are also supported, as is the original `New Text Document.txt` credential file. `.env` takes precedence over that text file. Environment variables take precedence over both. Secrets are never printed. Do not commit either credential file.

## User flow

Tap **Start** for the dashboard. Use the persistent **Assignments**, **My upload**, **Home** and **Help** buttons, or the commands below. Assignment lists have upcoming/overdue filters and five entries per page. Open an assignment to download materials or add homework. The upload screen supports removing individual staged files and reviewing before saving; discarding files or switching assignments requires confirmation. File changes invalidate old save/remove/discard buttons. Receipt summaries keep the next step visible; **Receipt details** shows full hashes and timestamp evidence.

1. Start a **private** chat with the bot and use `/connect`. Send your username and then password when prompted. Credential messages are deleted; Telegram bot chats are not end-to-end encrypted. The admin can use `/login` instead.
2. `/due` lists Moodle's cross-course action timeline, including overdue entries, with IDs. It paginates the calendar API. Completed work and assignments without due dates may not appear. The corresponding website view is **میز کار → گاه‌شمار فعالیت‌ها**.
3. `/assignment ID` shows the description, dates, submission status, attachment names, current limits and Moodle link. `/files ID` sends attached files.
4. `/upload ID` reads that assignment's actual submission rules. Send homework as **documents**, then `/review`, inspect the filenames and click **Save homework to Moodle**.
5. Read the returned Moodle status. If Moodle requires a separate final submission, use `/finalize ID`, read the declaration and explicitly confirm it. Final submission can lock editing. Some assignments submit immediately when files are saved.
6. `/cancel` removes staged files; `/logout` discards the bot session. Logging out of the bot does not invalidate other university browser sessions.
7. `/verify ID` safely rechecks the last receipt for this Telegram user, university account and assignment. After a restart or session expiry, reconnect to the same account first. This command only reads Moodle; it never replays an upload or final submission.
8. **Home → Notifications** or `/notifications` controls background alerts. The configured admin starts with alerts on and receives a one-time overview; other users explicitly enable them after connecting and start with a quiet baseline. Later scans: one new-assignment alert per new item, due-date change alerts, and reminders at 7 days / 3 days / 2 days / 1 day / 12 hours. Checks run about every five minutes and submitted assignments do not get reminders. Disabling alerts deletes the saved background login; signing out also disables alerts.

## Submission behavior and limits

- Re-reads Moodle's file count, per-file byte limit, total area limit and accepted formats before saving. Moodle remains the authority on deadlines, cutoff dates and permissions.
- Preserves existing files. Duplicate names, replacements, existing folders, and assignments with an online-text editor require the Moodle browser. It never silently removes an existing file.
- Uploads into Moodle's draft file area first, then saves the assignment form with its current tokens. Failed draft uploads do not trigger the save request.
- A successful HTTP response or matching filename does **not** count as success. The bot reloads the assignment, downloads the actual student submission files, and compares the full file set, byte counts and SHA-256 hashes with the reviewed manifest. Teacher attachments cannot satisfy this check. It reloads the page again to detect status/file changes during verification. Existing files are also fingerprinted and must be preserved.
- Only a fresh `submitted` Moodle status **and** matching content produces **TURNED IN — VERIFIED**. A matching draft produces **FILES SAVED AS DRAFT — NOT TURNED IN**. Missing/changed files, unsupported status, incomplete checks and ambiguous writes produce **OUTCOME UNCERTAIN — TURN-IN IS NOT VERIFIED**. These checks verify Moodle's stored content/state, not the academic correctness of the homework or its future grading.
- Before final submission, the displayed file hashes are checked again. The form's response cannot override a conflicting fresh assignment status.
- Receipts persist in `.data/receipts.sqlite3`, scoped to both Telegram user and university account. They contain file names, byte counts, hashes, state and timestamps, but no credentials or homework bytes. Logout removes the login session/staged files; it preserves these receipts for later verification.
- Each receipt records client request/acknowledgement/check times with milliseconds in Tehran time, upload round-trip durations from a monotonic clock, Moodle's reported last-modified/timing text and the verification response's HTTP Date header. Client timestamps and Date headers are **not** an exact server acceptance timestamp or proof of meeting a deadline. Moodle's displayed timing is reported as evidence; the bot does not infer an on-time submission from upload start time.
- If a save/final-submit response is lost, the bot performs up to three read-only checks and never automatically resends the write. It blocks new writes for an unresolved attempt, including after restart. Use `/verify ID` and inspect Moodle directly. An uncertain result may require manual handling; do not assume failure and blindly upload again.
- The hosted Telegram Bot API allows downloading user documents up to 20 MiB and sending files up to 50 MiB. These limits can be lower than Moodle's. Larger files require Moodle directly. A self-hosted Telegram Bot API server could be a future extension; this implementation uses the hosted service.
- The bot stages at most 20 documents per user, uses isolated temporary directories, and cleans them on cancel, logout, normal shutdown and 30-minute inactivity expiry. A hard process kill can leave temporary files in the OS temp directory.
- Interactive sessions remain in memory and expire. Notification subscribers explicitly allow an encrypted persistent background login so alerts survive restart/inactivity. It lives in `.data/notifications.sqlite3`; `.data/notifications.key` contains the encryption key. Keep both private and back up the key separately; losing it makes saved logins unusable. Disabling alerts/signing out deletes the saved login. The configured admin's first alert subscription uses the existing server credentials. Do not enable request/update logging: Telegram request URLs include the bot token and incoming messages may contain passwords.
- Group chats are ignored. No cross-user access to sessions or staged files. The bot processes updates sequentially, and mutations also use per-user locks.
- Public access here means anyone can connect their own account. This is a small single-process deployment, not a hardened public hosting service; add rate limits and capacity controls before advertising it broadly.
- This uses Moodle's session-protected web endpoints, not a Moodle administrator token. Changes to the university SSO/theme/Moodle may need adapter updates. Authentication challenges require the browser.

## Verification

Read-only live check (does not upload or submit homework):

```powershell
python -X utf8 vc_client.py --check 123
```

Unit tests (mock all submission requests):

```powershell
python -m pip install pytest pytest-asyncio
python -m pytest -q
```

Live authentication, calendar discovery, attachment retrieval and submission-rule parsing can be checked without changing coursework. An end-to-end upload/final-submission test requires a disposable assignment and has deliberately not been run against real homework.

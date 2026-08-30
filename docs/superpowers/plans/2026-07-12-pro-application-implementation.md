# Secure Pro Application Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Gmail-backed-by-human, QQ-verified Pro application workflow while permanently keeping local Agent access exclusive to QQ `<configured-qq-id>`.

**Architecture:** A `pro_application` AstrBot plugin owns the SQLite state machine and all applicant/reviewer commands. `draw_command` reads the same local, expiry-aware Pro membership database on every request. `claude_code_agent` and its `TrustedPolicy` are intentionally not widened.

**Tech Stack:** Python 3.12, SQLite standard library, AstrBot plugin filters, `secrets`, `hashlib`, `unittest`.

## Global Constraints

- Gmail address: `<configured-contact-email>`; the bot stores no Gmail token, email body, or inbox data.
- Reviewer QQ: `<configured-qq-id>`; only this account may approve, deny, list pending, or revoke.
- Public Pro never grants local Agent access.
- Applications expire after 72 hours; verification codes expire after 10 minutes and three failed attempts.
- Default Pro duration is 90 days; approval duration must be 1–365 days.

---

### Task 1: Secure membership state machine

**Files:**
- Create: `astrbot/data/plugins/pro_application/pro_store.py`
- Create: `tests/test_pro_store.py`

**Interfaces:**
- `ProStore(path: Path, reviewer_id: str)`
- `create_application(qq_id: str, now: float) -> Application`
- `mark_sent(application_id: str, qq_id: str, now: float) -> str`
- `approve(application_id: str, reviewer_id: str, days: int, now: float) -> str`
- `verify(qq_id: str, code: str, now: float) -> str`
- `is_active_pro(qq_id: str, now: float) -> bool`
- `revoke(qq_id: str, reviewer_id: str, now: float) -> bool`

- [ ] Write tests for same-QQ application confirmation, reviewer-only approval, hash-only code storage, 10-minute expiry, three-attempt lockout, 72-hour application expiry, 1–365 day duration, expiry, and revocation.
- [ ] Run `python tests\test_pro_store.py`; expect an import failure before implementation.
- [ ] Implement a single SQLite table for minimum metadata: application ID, QQ ID, state, created/expires timestamps, reviewer, code hash, attempts, Pro expiry, and event type/time. Clean stale rows before each operation.
- [ ] Run `python tests\test_pro_store.py`; expect all tests to pass.
- [ ] Commit with `feat: add secure Pro membership store`.

### Task 2: Applicant and reviewer QQ commands

**Files:**
- Create: `astrbot/data/plugins/pro_application/main.py`
- Create: `astrbot/data/plugins/pro_application/metadata.yaml`
- Create: `tests/test_pro_application_plugin.py`

**Interfaces:**
- Applicant commands: `/pro apply`, `/pro sent <id>`, `/pro status`, `/pro verify <code>`.
- Reviewer commands: `/pro pending`, `/pro approve <id> [days]`, `/pro deny <id>`, `/pro revoke <qq>`.

- [ ] Write tests proving only the reviewer can run approval commands, applicants can see only their own status, `/pro apply` returns the fixed Gmail address and a safe template, and replies never include a stored hash, email body, or local path.
- [ ] Run `python tests\test_pro_application_plugin.py`; expect an import failure before implementation.
- [ ] Implement strict token parsing. `/pro apply` creates an ID; `/pro sent` only transitions the applicant's own ID; `/pro approve` creates a one-time code bound to the original QQ; `/pro verify` activates only that QQ.
- [ ] Run `python tests\test_pro_application_plugin.py`; expect all tests to pass.
- [ ] Commit with `feat: add QQ Pro application commands`.

### Task 3: Dynamic Pro drawing authorization

**Files:**
- Create: `astrbot/data/plugins/draw_command/pro_access.py`
- Modify: `astrbot/data/plugins/draw_command/main.py`
- Modify: `tests/test_draw_plugin.py`

**Interfaces:**
- `is_active_pro(qq_id: str, db_path: Path, now: float | None = None) -> bool`
- `DrawCommand._is_pro(sender_id: str) -> bool`

- [ ] Write tests proving a database-approved, unexpired user may draw; expired/revoked users cannot trigger the image proxy; the configured owner bootstrap allowlist still works.
- [ ] Run `python tests\test_draw_plugin.py`; expect the new dynamic-access tests to fail before implementation.
- [ ] Implement a read-only SQLite lookup with fail-closed behavior for absent/corrupt data. Do not import `pro_application` as a sibling AstrBot plugin.
- [ ] Run `python tests\test_draw_plugin.py` and `python tests\test_draw_plugin_isolated_import.py`; expect all tests to pass.
- [ ] Commit with `feat: honor approved Pro drawing access`.

### Task 4: Pro-to-Agent non-escalation proof

**Files:**
- Modify: `tests/test_agent_access_policy.py`
- Modify: `tests/test_agent_integration.py`

- [ ] Write a fixture-backed test that activates a public Pro account and proves `/agent` is still denied for it.
- [ ] Run the targeted Agent tests; expect the new proof to fail before wiring the membership fixture.
- [ ] Add only test fixtures; do not change `TrustedPolicy`, `trusted_pro_user_ids`, or `claude_code_agent` authorization.
- [ ] Run `python tests\test_agent_access_policy.py` and `python -m unittest discover -s tests -p 'test_agent_integration.py'`; expect public Pro denied and `<configured-qq-id>` allowed.
- [ ] Commit with `test: preserve exclusive Trusted Pro Agent access`.

### Task 5: Deployment verification

**Files:**
- Modify: ignored runtime configuration only when necessary; do not stage it.

- [ ] Run `python -m unittest discover -s tests -p 'test_*.py'`; expect exit code 0.
- [ ] Restart AstrBot; verify loopback ports 6185/6199, QQ WebSocket reconnection, and no `pro_application`, `draw_command`, or `claude_code_agent` import errors.
- [ ] Push `main` and verify `tomerose/qqbot-private-backup` remains private.

# Trusted Pro Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver reliable Pro drawing and a Trusted Pro local Agent that can research public GitHub sources, generate and verify Word artifacts, fall back between backends, and deliver files without exposing the host.

**Architecture:** Keep authorization, task state, and delivery in the AstrBot plugin. Add pure policy modules for Trusted Pro permissions, backend health/failover, artifact collection, and Word validation. All external output passes deterministic validation before OneBot delivery.

**Tech Stack:** Python 3.12, AstrBot 4.26.5, asyncio subprocess, Claude Code, Codex CLI, WorkBuddy CLI, GitHub CLI/HTTPS, python-docx, LibreOffice, Pillow, SQLite, unittest.

## Global Constraints

- Trusted Pro is configured locally and currently contains only QQ `1211000567`.
- No model, prompt, chat message, memory, or generated file can change permission membership.
- Never expose absolute paths, credentials, cookies, private chats, contacts, environment secrets, or raw stack traces to QQ.
- Never modify Claude Code, Codex, WorkBuddy, or cc-connect global configuration.
- Never claim task completion until artifact verification and QQ delivery both succeed.
- Work in the existing live checkout because successful increments must be loaded into the running QQBot.

---

### Task 1: Repair isolated Pro drawing plugin loading

**Files:**
- Modify: `astrbot/data/plugins/draw_command/main.py`
- Modify: `astrbot/data/plugins/draw_command/draw_core.py`
- Modify: `tests/test_draw_plugin.py`
- Create: `tests/test_draw_plugin_isolated_import.py`

**Interfaces:**
- Produces: `parse_pro_user_ids(value) -> tuple[str, ...]`
- Guarantees: `draw_command` imports without any sibling plugin on `sys.path`.

- [ ] Write an isolated-import test that removes the plugins root from `sys.path` and reproduces `ModuleNotFoundError`.
- [ ] Run `python tests/test_draw_plugin_isolated_import.py` and confirm RED.
- [ ] Move the small immutable Pro-ID parser into `draw_core.py` and remove the sibling-plugin import.
- [ ] Update plugin tests to use local Pro membership.
- [ ] Run draw tests and confirm GREEN.
- [ ] Restart AstrBot, verify plugin load log, proxy health, loopback ports, and a real image-generation smoke.
- [ ] Commit the drawing repair.

### Task 2: Add Trusted Pro hard policy

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/access_policy.py`
- Create: `astrbot/data/plugins/claude_code_agent/trusted_policy.py`
- Modify: `astrbot/data/plugins/claude_code_agent/_conf_schema.json`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `tests/test_agent_access_policy.py`
- Create: `tests/test_trusted_policy.py`

**Interfaces:**
- Produces: `TrustedCapability`, `TrustedDecision`, `assess_trusted_request(sender_id, task, work_dir)`.
- Guarantees: hard-denied system/privacy actions cannot be approved.

- [ ] Write RED tests for Trusted Pro membership, system roots, credentials, allowlist mutation, GitHub write, external send, and safe project/document work.
- [ ] Implement fail-closed policy with immutable IDs and canonical path checks.
- [ ] Apply the policy before planning and again before process launch.
- [ ] Run policy and Agent integration tests.
- [ ] Commit the policy increment.

### Task 3: Add backend health and safe fallback

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/backend_health.py`
- Modify: `astrbot/data/plugins/claude_code_agent/backend_router.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Create: `tests/test_backend_health.py`
- Modify: `tests/test_backend_router.py`
- Modify: `tests/test_agent_integration.py`

**Interfaces:**
- Produces: `BackendHealth`, `probe_backend(name, timeout)`, `fallback_order(step, preferred, health)`.
- Guarantees: fallback occurs only before side effects and at most once per alternate backend.

- [ ] Write RED tests for Claude failure to Codex, Codex failure to WorkBuddy eligibility, no retry after side effects, and bounded probes.
- [ ] Implement cached health probes and deterministic task-aware routing.
- [ ] Wire health into execution without changing global CLI configuration.
- [ ] Run router and integration tests.
- [ ] Commit the fallback increment.

### Task 4: Make artifact discovery deterministic

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/artifact_contract.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Create: `tests/test_artifact_contract.py`
- Modify: `tests/test_task_verifier.py`
- Modify: `tests/test_agent_integration.py`

**Interfaces:**
- Produces: `ArtifactExpectation`, `collect_new_artifacts(job_dir, work_dir, snapshot)`.
- Guarantees: only newly created regular files under approved roots can be copied into `outputs`.

- [ ] Write RED tests for a Word file created in the selected work directory, pre-existing files, symlinks, sensitive names, and outside-root files.
- [ ] Snapshot approved roots before execution and collect only new/changed expected artifacts after success.
- [ ] Require expected artifacts before task completion.
- [ ] Run artifact, verifier, DLP, and integration tests.
- [ ] Commit the artifact increment.

### Task 5: Add GitHub read-only research and Word QA

**Files:**
- Create: `astrbot/data/plugins/claude_code_agent/research_policy.py`
- Create: `astrbot/data/plugins/claude_code_agent/word_quality.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/task_planner.py`
- Create: `tests/test_research_policy.py`
- Create: `tests/test_word_quality.py`
- Modify: `tests/test_delivery_dlp.py`

**Interfaces:**
- Produces: `ResearchRequest`, `validate_public_github_action(args)`, `WordQualityResult`, `verify_word_document(path, qa_dir)`.
- Guarantees: GitHub research is read-only and Word completion requires structure/privacy/render checks.

- [ ] Write RED tests that allow public search/view/shallow clone but reject GitHub mutations and private-token exposure.
- [ ] Write RED tests for valid Word, corrupt Word, empty Word, private metadata, path leakage, missing sources, and failed render.
- [ ] Implement read-only research command validation and explicit research prompt contract.
- [ ] Implement OOXML checks, metadata scrubbing, LibreOffice render with bounded timeout, and DLP revalidation.
- [ ] Run research, Word, DLP, planner, and integration tests.
- [ ] Commit the research/Word increment.

### Task 6: Runtime validation, documentation, and private backup

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`
- Modify: `astrbot/data/plugins/draw_command/metadata.yaml`
- Modify: `docs/superpowers/plans/2026-07-12-trusted-pro-agent.md`

- [ ] Run `python -m unittest discover -s tests` with zero failures.
- [ ] Run `python -m py_compile` for all changed Python modules.
- [ ] Run real read-only backend probes for Claude, Codex, and WorkBuddy where available.
- [ ] Generate a safe Word smoke artifact, scrub it, render every page, and inspect the PNGs.
- [ ] Restart AstrBot and verify ports `3000`, `6185`, `6199`, and `8766` are loopback-only.
- [ ] Verify startup logs contain no `draw_command` or Agent import errors.
- [ ] Commit final docs and push `main` to the private GitHub repository.

## Plan self-review

- Spec coverage: drawing, Trusted Pro policy, backend fallback, artifacts, GitHub research, Word QA, privacy, runtime, and backup each have a testable task.
- Placeholder scan: no deferred implementation placeholders are present.
- Interface consistency: policies are pure modules consumed by `main.py`; artifact and Word validators return explicit results.
- Scope: global agent configurations and unrelated AstrBot plugins remain untouched.


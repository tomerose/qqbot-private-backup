# QQBot Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove verified process leaks and close privacy, cancellation, network, logging, and configuration risks without changing the bot's user-facing Agent workflow.

**Architecture:** Keep authorization and delivery in the existing plugin, replace recursive workspace snapshots with explicit referenced-file extraction, and move Windows process-tree termination into a tested helper. Disable the unused child-process WebUI through its existing plugin configuration path and harden runtime configuration at the boundary.

**Tech Stack:** Python 3.12, AstrBot 4.26.5, asyncio, OneBot/aiocqhttp, PowerShell, Windows ACL and Scheduled Tasks.

## Global Constraints

- Preserve Claude Code and cc-connect global configuration.
- Keep QQ `1211000567` as the only full Agent owner.
- Do not add dependencies.
- Do not print or rotate unrelated API credentials.
- Keep QQ file uploads limited to validated regular files.

---

### Task 1: Replace recursive workspace scanning

**Files:**
- Modify: `tests/test_agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`

**Interfaces:**
- Produces: `referenced_workspace_files(text, root, max_files, max_file_bytes) -> list[Deliverable]`
- Removes: request-wide workspace snapshots and changed-file scans.

- [ ] Add tests proving an explicitly referenced workspace file is returned and an outside path is rejected.
- [ ] Run tests and confirm the new tests fail.
- [ ] Implement bounded referenced-file extraction and integrate it before path redaction.
- [ ] Run the complete test file and confirm it passes.

### Task 2: Kill complete Agent process trees

**Files:**
- Modify: `tests/test_agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`

**Interfaces:**
- Produces: `build_process_tree_kill_command(pid) -> list[str]`.

- [ ] Add a failing test requiring `taskkill.exe /PID <pid> /T /F`.
- [ ] Implement the helper and use it from cancellation and timeout paths.
- [ ] Verify fallback termination still handles command failure.

### Task 3: Close intermediate-output and WebUI exposure

**Files:**
- Modify: `astrbot/data/cmd_config.json`
- Modify: `astrbot/data/config/astrbot_plugin_aiocensor_config.json`
- Modify: `astrbot/data/plugins/astrbot_plugin_aiocensor/main.py`

**Interfaces:**
- AIOCensor `webui.enable=false` prevents process creation.

- [ ] Bind AstrBot Dashboard to localhost, disable raw tool results, enable intermediate buffering and file logging.
- [ ] Add the AIOCensor WebUI enable guard and disable the WebUI in runtime config.
- [ ] Parse both JSON files and import both plugins.

### Task 4: Clean runtime and configuration state

**Files:**
- Delete: `data/cmd_config.json`
- Modify ACLs for active sensitive JSON files.

- [ ] Stop AstrBot and terminate only verified orphan multiprocessing children listening on 8192.
- [ ] Remove the stale root configuration.
- [ ] Restrict active sensitive files to the current user, SYSTEM and Administrators.
- [ ] Restart and verify 8192 is closed, 6185/6199 are local, and QQ is connected.

### Task 5: Upgrade and verify Codex

**Files:** None.

- [ ] Query the npm registry for the current `@openai/codex` version.
- [ ] Upgrade the global package without changing model configuration.
- [ ] Verify version, login, and a minimal `codex exec` response.

### Task 6: Final verification

- [ ] Run all 15+ unit tests and Python compilation.
- [ ] Verify plugin imports and runtime JSON parsing.
- [ ] Verify active processes, ports, logs, ACLs and QQ WebSocket.
- [ ] Confirm Claude remains the default QQBot backend.

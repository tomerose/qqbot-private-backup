# Multi-Backend QQ Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add owner-only full-permission Claude/Codex/WorkBuddy task execution with selectable working directory and QQ image/file delivery.

**Architecture:** Keep AstrBot message authorization and lifecycle control in `main.py`. Put deterministic backend command construction, result parsing, working-directory validation, and output discovery in `agent_core.py`. Each job gets a private return directory while the CLI runs from the owner-selected directory.

**Tech Stack:** Python 3, AstrBot Star plugin, asyncio subprocess, Claude Code CLI, Codex CLI, WorkBuddy CodeBuddy CLI.

---

### Task 1: Define core behavior with tests

**Files:**
- Modify: `tests/test_agent_core.py`
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`

Add failing tests for backend aliases, unrestricted command flags, work-directory validation, backend output parsing, and bounded attachment discovery. Run the test file and confirm the new tests fail before implementation.

### Task 2: Implement the unified execution core

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/agent_core.py`

Implement backend command builders without shell concatenation, preserve environment pass-through, create per-job output directories, parse each CLI result format, and safely list deliverables.

### Task 3: Wire AstrBot commands and attachments

**Files:**
- Modify: `astrbot/data/plugins/claude_code_agent/main.py`
- Modify: `astrbot/data/plugins/claude_code_agent/_conf_schema.json`
- Modify: `astrbot/data/config/claude_code_agent_config.json`
- Modify: `astrbot/data/plugins/claude_code_agent/README.md`

Add backend and cwd commands, default Claude routing, full-permission execution for the sole owner, image/file component delivery, and concise Chinese status/error messages.

### Task 4: Verify and restart

Run unit tests and Python compilation, check all CLI versions, execute one minimal non-mutating prompt per backend, restart AstrBot, and confirm its QQ WebSocket is established.

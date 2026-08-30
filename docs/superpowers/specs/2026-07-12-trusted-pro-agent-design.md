# Trusted Pro Agent and Reliable Artifact Delivery Design

## Goal

Make QQ `<configured-qq-id>` able to request complete, high-quality local Agent tasks, including public GitHub research and Word document delivery, while keeping system security, credentials, private data, and irreversible external actions protected.

## Runtime model

- Ordinary users retain chat and explicit voice interaction only.
- Pro users retain Pro content capabilities such as controlled image generation.
- Trusted Pro is a machine-local immutable allowlist containing only `<configured-qq-id>`.
- Trusted Pro may read, create, modify, build, test, and render inside approved project roots.
- Credential access, browser/session data, system directories, privilege changes, services, scheduled tasks, security controls, and changes to the allowlists are always denied.
- Deletion of existing content, GitHub writes, deployment, external messaging, publishing, and system-level installation require an exact one-time approval and may still be rejected by hard policy.

## Execution architecture

Each job owns `work`, `references`, `outputs`, and `qa` directories. The orchestrator, not an LLM prompt, controls those paths and verifies every produced artifact.

Backend order is task-aware:

1. Claude Code when healthy.
2. Codex after a real health probe when Claude is unavailable before side effects.
3. WorkBuddy only for desktop-oriented or last-resort execution.

Retries are forbidden after a step reports side effects. A backend succeeds only when its process exits successfully, returns readable output, and produces every expected artifact.

## GitHub research

Public GitHub research is read-only by default. The task prompt may use repository search, release metadata, README files, and shallow public clones inside the job reference directory. GitHub push, issue, PR, release, workflow, or repository mutation is never part of a research task and requires a separately approved high-impact step.

## Word quality contract

A high-quality Word task must:

- place the final `.docx` in the job output directory;
- contain a title, dated scope, structured headings, source list, URLs, and retrieval dates when research is requested;
- pass ZIP/OOXML structural validation and local DLP;
- scrub personal and machine metadata;
- render through LibreOffice to page PNGs when the renderer is available;
- reject empty, corrupt, visually unrenderable, path-leaking, or sensitive documents;
- report completion only after QQ confirms file delivery.

## Pro drawing

The drawing plugin owns its Pro allowlist locally and cannot import another AstrBot plugin. It accepts private `/draw` or group `/draw` with a real bot mention, calls only the loopback Gemini proxy, sanitizes the generated image, sends one QQ image, and deletes the temporary file after delivery.

## Acceptance criteria

- AstrBot logs show `draw_command v2.0.0` loaded without import errors.
- A real Gemini image request returns a valid image and the QQ adapter receives an image component for Trusted Pro.
- Claude-unavailable simulation selects Codex; Codex-unavailable simulation selects WorkBuddy only when eligible.
- Artifact tasks that write outside `outputs` are recovered into `outputs` only when the artifact is newly created inside an approved job/work root.
- A generated Word fixture passes structure, privacy, render, DLP, and delivery tests.
- Full unit tests pass and runtime ports remain loopback-only.

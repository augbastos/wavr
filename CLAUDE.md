# Wavr — CLAUDE.md

**Read [AGENTS.md](AGENTS.md) first. It is the canonical contract** — what the project is,
the layout, the verified commands, the invariants, what is never committed, and the
standard work is judged against. This file adds only what is specific to Claude Code, and
it is deliberately short: anything load-bearing belongs in AGENTS.md, where every agent and
every human reads it.

If this file ever contradicts AGENTS.md, AGENTS.md wins and this file is the bug.

## Claude-specific

- **Specialists.** This repository has domain agents defined outside it — sensor fusion,
  computer vision, spatial geometry, network protocol discovery, the Capacitor shell, the
  mobile companion UX. Route domain work to the owner rather than doing it generically.
  They live in the operator's own agent directory, not in this repository, so their exact
  names are an environment detail rather than a project fact.
- **Proportional effort.** A one-line fix, a documentation change and a question need no
  fan-out. A broad audit or a large refactor is what a fleet is for. Do not spawn agents
  in proportion to how important the task feels.
- **Verify a delegated result before integrating it.** A worker's report is a claim, not
  proof: run the tests yourself, read the diff, and check that the canonical and served
  copies of a file are both updated.

## Platform notes that bite here

- **Write files with the file-writing tool, never a heredoc or a shell redirect** — on
  Windows those have produced zero-byte files in this repository.
- **Environment variables holding paths go through PowerShell, not Git Bash** — MSYS
  rewrites forward slashes and corrupts them.
- **Anchors in patch scripts must normalise `\r\n` to `\n`** before matching. The tree is
  mixed CRLF/LF, and a patch anchored on the wrong line ending silently matches nothing.
- **Never kill the Core by process name.** A running instance may be somebody's live
  install. Kill by PID.

## State (2026-09-09 — update when it changes)

The tree carries: the Space / people / device-function model (ADR-0009) and person-role
authorisation (ADR-0011); consent tiers enforced at read time; the multidevice Tauri shell
with a pinned certificate; a provider-agnostic narrator; non-biometric who-is-home; agentic
routines, guest mode and a transparency endpoint; MCP-over-HTTP read transport (ADR-0008);
`net_doctor` deep network diagnosis with honest cause discrimination and an opt-in,
default-OFF MAC-redacted report; the Capacitor Android companion under `mobile/` with its
Kotlin plugins and certificate pinning; the Android Core launcher under `core-launcher/`
(ADR-0010); and the spatial layer.

Work in progress: camera calibration and homography (`calib_store.py`, `localize.py` and
their tests). Do not revert it or fold it into an unrelated commit.

There is no hosted online demo, by design.

# Graphify Setup Design — 2026-07-03

## Context

Graphify (`pip`/`uv tool install graphifyy`, CLI `graphify`) builds a queryable knowledge graph over a codebase — code, SQL schemas, docs, papers — using Tree-sitter (free, local, no API key) for code structure and an LLM (Anthropic/Gemini/OpenAI/Ollama, costs money) for semantic extraction of docs/comments/design rationale. Output: `graph.html` (interactive viz), `GRAPH_REPORT.md` (highlights, key concepts, suggested questions), `graph.json` (full queryable graph). Optional flags `--obsidian` / `--wiki` can also emit an Obsidian vault or markdown wiki as a side output.

Augusto wants this to help navigate and optimize his real repos, starting with a pilot before committing further.

## Goal

Stand up Graphify on Wavr first as a pilot. If the pilot produces a genuinely useful, accurate map of the codebase, immediately chain into doing the same for Lucky Cat in the same effort (not a separate future session).

## Decisions

1. **Repo order:** Wavr (`C:\IA\wavr`) first — small, clean, single-language (Python/FastAPI), already well-documented (README + PRODUCT.md), low risk to validate the tool. Lucky Cat (`C:\IA\lucky_cat`) follows immediately after, only if Wavr's pilot succeeds.

2. **Success criteria for the Wavr → Lucky Cat chain:** the Wavr `GRAPH_REPORT.md` and `graph.html` must produce a coherent, accurate map of Wavr's real architecture (FusionEngine, SourceManager, the pluggable `SensorSource` protocol, the privacy boundaries) with no garbage or hallucinated nodes. If the output is incoherent, garbled, or clearly wrong about the codebase, stop and reassess before touching Lucky Cat.

3. **Output stays separate from the Obsidian vault.** No `--obsidian` or `--wiki` flags. Output lands in each repo's own `graphify-out/` folder (default location) — `graph.html`, `graph.json`, `GRAPH_REPORT.md`. The vault's `raw/wiki/output` structure (reorganized the same day as this spec) stays untouched by Graphify. If a genuinely useful finding surfaces in a `GRAPH_REPORT.md` (e.g., a "god node," a surprising cross-module connection), it gets linked manually into the relevant project's wiki Hub "Docs de Apoio" section later — not automated.

4. **Ongoing maintenance: install the git hook from the start in both repos.** `graphify hook install` — every commit triggers an incremental, AST-only reprocessing (free, no LLM call) automatically. This is a standing process change to real dev repos, not a one-off — Augusto chose this over a manual/one-time-only approach after weighing both.

5. **LLM backend: Anthropic**, using the existing `ANTHROPIC_API_KEY` already available in this environment (Claude Code ecosystem is already in use everywhere else) — no new key setup needed. Code-only extraction (Tree-sitter) is always free regardless of backend; the backend is only invoked for doc/comment/design-rationale semantic extraction. Use Graphify's `--token-budget` flag to control chunk sizes if a run's cost/scope needs bounding — decide per-run, not upfront, since Wavr's docs are light (README + PRODUCT.md) and Lucky Cat's are heavier (more migrations, more docs).

6. **`graphify-out/` gets added to `.gitignore`** in each repo — it's a generated artifact, not source, and would otherwise pollute the git history on every hook-triggered commit-time regeneration.

## Out of Scope

- Any repo beyond Wavr and Lucky Cat (Replyon, Passer, Festa, WataMote, tillr-frontend, ownly-backend, etc.) — not part of this pass.
- Building a custom UI or dashboard around Graphify's output — using its own `graph.html`/`GRAPH_REPORT.md`/`graphify query` as-is.
- Automating the "link a finding into the wiki Hub" step — that stays a manual, human-judgment action.

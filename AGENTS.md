# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Python 3 standard library only, no dependencies. Tests: `python3 -m unittest discover -s tests`.
- Hard rule: `~/.no-mistakes/` is read-only. Open the database only through `connect_readonly` in `nmn/state.py`. Never call the `no-mistakes` CLI, the daemon, or a gate from this tool.
- No outbound network and no model calls, ever. The merge gate greps code and fixtures for provider names and non-loopback URLs, so fixtures drop `model_provider` and the page avoids SVG namespace URLs. Regenerate fixtures with `tests/make_fixture.py`, which sanitizes them.
- `nmn/model.py` is pure (snapshot in, tabs out); disk access stays in `nmn/state.py`.
- no-mistakes records an agent invocation only when it finishes; the live agent comes from the step log (`<tool> started pid=`) plus `config.yaml`.
- UI text is French.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

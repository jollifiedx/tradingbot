---
name: backend-venv-interpreter
description: Bare `python` on PATH is a different interpreter with none of the project deps — always run pytest/ruff/mypy through backend/.venv/Scripts/python.exe
metadata:
  type: project
---

All backend checks must run through the project venv interpreter
(`backend/.venv/Scripts/python.exe -m pytest|ruff|mypy`). The bare `python` on PATH resolves to
a different install where `apscheduler`, `exchange_calendars`, `asyncpg` etc. are absent.

**Why:** a bare `python -c "import apscheduler"` reports ModuleNotFoundError and reads as "the
dependency is missing / needs installing" when it is in fact already installed and declared in
`pyproject.toml`. That misread leads to a pointless `pip install` into the wrong environment.

**How to apply:** before concluding a dependency is missing, re-check with the venv interpreter.
VERIFIED sections must quote output from that interpreter, not the PATH one.

# Contributing to BotEnclave

## Development setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Workflow

- Development is test-driven: new behaviour starts with a test, and security
  features are only complete when they also have negative tests proving that
  unauthorised paths are refused (another user's bot, another bot's process,
  out-of-range paths, malformed arguments).
- CI runs the test suite, ruff, mypy, a dependency audit (pip-audit), and a
  secret scan (gitleaks). All of them must pass before a change is merged.

## Commit messages

- One purpose per commit.
- Subject: `<prefix>: <summary>`, with a lowercase prefix such as `add`,
  `fix`, `update`, `change`, `docs`, `refactor`, or `delete`.
- Add a body when the reason is not obvious from the subject, and explain
  *why* the change is needed rather than restating the diff.

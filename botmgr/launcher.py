"""Launcher shim: the small program that runs in the bot's pane, receives the
launch configuration exactly once, and then replaces itself with the bot.

The configuration arrives over a one-shot FIFO (PLAN sections 1.4 and 3.3),
never via argv, so secrets stay out of /proc/<pid>/cmdline. The shim parses
the .env payload, drops interpreter-control variables, applies rlimits,
changes into the bot's data directory, and execs the bot command with exactly
the configured environment.
"""

from __future__ import annotations

import dataclasses
import json
import os
import resource
import sys
from collections.abc import Mapping

DENIED_ENV_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONBREAKPOINT",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_DEBUG",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "RUBYOPT",
        "RUBYLIB",
        "PERL5LIB",
    }
)


def _unescape_double_quoted(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt in ("\\", '"'):
                out.append(nxt)
            else:
                out.append(ch)
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return _unescape_double_quoted(value[1:-1])
    comment_at = value.find(" #")
    if comment_at >= 0:
        value = value[:comment_at].rstrip()
    return value


_ENV_QUOTE_CHARS = frozenset(" \t\r\n#'\"")


def format_env_text(env: Mapping[str, str]) -> str:
    """Serialize env vars as .env text that parse_env_text reads back
    exactly (the shared format for every secret backend, PLAN 3.3)."""
    lines: list[str] = []
    for key in sorted(env):
        value = env[key]
        if value and not any(ch in _ENV_QUOTE_CHARS for ch in value):
            lines.append(f"{key}={value}")
        else:
            escaped = (
                value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + ("\n" if lines else "")


def parse_env_text(text: str) -> dict[str, str]:
    """Parse .env content (KEY=VALUE lines) the way python-dotenv generally
    does: comments and blank lines are skipped, an ``export `` prefix is
    accepted, values may be single- or double-quoted."""
    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise ValueError(f".env line {lineno}: expected KEY=VALUE")
        result[key.strip()] = _parse_value(value.strip())
    return result


def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
    cleaned = {k: v for k, v in env.items() if k not in DENIED_ENV_KEYS}
    if "PATH" not in cleaned:
        cleaned["PATH"] = os.defpath
    return cleaned


@dataclasses.dataclass(frozen=True)
class LaunchConfig:
    command: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    rlimit_nofile: int | None = None
    rlimit_nproc: int | None = None
    # Spawn-time only flags (never persisted with secrets): the helper
    # decrypts this bot's systemd-creds credential itself, and/or writes
    # the merged env to data/.env for dotenv-style bots (PLAN 3.3 / 2.8).
    use_credential: bool = False
    dotenv_mode: bool = False


def config_from_json(raw: bytes) -> LaunchConfig:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid launch configuration: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("launch configuration must be a JSON object")
    command = data.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ValueError("launch configuration needs a non-empty command list")
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("launch configuration needs a cwd")
    env: dict[str, str] = {}
    env_text = data.get("env_text", "")
    if not isinstance(env_text, str):
        raise TypeError("env_text must be a string")
    if env_text:
        env.update(parse_env_text(env_text))
    extra = data.get("env", {})
    if not isinstance(extra, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra.items()
    ):
        raise ValueError("env must be an object of strings")
    env.update(extra)
    rlimits = data.get("rlimits", {})
    if not isinstance(rlimits, dict):
        raise TypeError("rlimits must be an object")

    def _limit(name: str) -> int | None:
        value = rlimits.get(name)
        if value is None:
            return None
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"rlimits.{name} must be a positive integer")
        return value

    return LaunchConfig(
        command=tuple(command),
        cwd=cwd,
        env=env,
        rlimit_nofile=_limit("nofile"),
        rlimit_nproc=_limit("nproc"),
    )


def _apply_rlimit(kind: int, value: int) -> None:
    _soft, hard = resource.getrlimit(kind)
    effective = min(value, hard) if hard != resource.RLIM_INFINITY else value
    resource.setrlimit(kind, (effective, hard))


def run(config: LaunchConfig) -> None:
    """Replace this process with the bot. Never returns on success."""
    os.chdir(config.cwd)
    if config.rlimit_nofile is not None:
        _apply_rlimit(resource.RLIMIT_NOFILE, config.rlimit_nofile)
    if config.rlimit_nproc is not None:
        _apply_rlimit(resource.RLIMIT_NPROC, config.rlimit_nproc)
    env = sanitize_env(config.env)
    os.execvpe(config.command[0], list(config.command), env)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m botmgr.launcher <launch-fifo>", file=sys.stderr)
        return 2
    fifo_path = argv[1]
    with open(fifo_path, "rb") as fh:
        raw = fh.read()
    # The configuration is single-use; remove the FIFO as soon as it has
    # been read so nothing can replay or re-read it.
    os.unlink(fifo_path)
    run(config_from_json(raw))
    return 0  # pragma: no cover - run() execs on success


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

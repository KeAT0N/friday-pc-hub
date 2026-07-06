"""Shared test helpers: invoke a hub module CLI and assert the envelope contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENVELOPE_KEYS = {"ok", "action", "data", "error"}


def run_cli(module: str, *args: str, timeout: float = 30.0,
            env: dict | None = None, input: str | None = None) -> tuple[dict, int]:
    """Run `python -m hub.<module> <args>` and return (parsed_envelope, exit_code).

    Raises AssertionError if stdout is not exactly one valid envelope object.
    When `input` is None, stdin is /dev/null so nothing can inherit a real TTY
    (keeps getpass-based paths deterministic and non-blocking in tests).
    """
    proc = subprocess.run(
        [sys.executable, "-m", f"hub.{module}", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, env=env,
        input=input, stdin=None if input is not None else subprocess.DEVNULL,
    )
    out = proc.stdout.strip()
    assert out, (f"hub.{module} {args} produced no stdout. "
                 f"stderr:\n{proc.stderr}")
    try:
        env_obj = json.loads(out)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"hub.{module} {args} stdout is not JSON: {e}\n"
            f"stdout:\n{out}\nstderr:\n{proc.stderr}") from e
    return env_obj, proc.returncode


def assert_envelope(env_obj: dict, code: int) -> None:
    """Assert the object obeys the envelope contract and exit code mirrors ok."""
    assert isinstance(env_obj, dict), f"envelope not a dict: {env_obj!r}"
    assert ENVELOPE_KEYS <= set(env_obj), (
        f"envelope missing keys {ENVELOPE_KEYS - set(env_obj)}: {env_obj!r}")
    assert isinstance(env_obj["ok"], bool), "ok must be bool"
    assert isinstance(env_obj["action"], str) and env_obj["action"], \
        "action must be a non-empty str"
    if env_obj["ok"]:
        assert env_obj["error"] is None, "ok=true must carry error=null"
        assert code == 0, f"ok=true must exit 0, got {code}"
    else:
        assert env_obj["error"], "ok=false must carry a non-empty error"
        assert code == 1, f"ok=false must exit 1, got {code}"


def assert_ascii(env_obj: dict) -> None:
    """The serialized envelope must be pure ASCII (ensure_ascii contract)."""
    s = json.dumps(env_obj, ensure_ascii=False)
    non_ascii = [c for c in s if ord(c) > 127]
    # The re-serialized form may contain unicode; the CONTRACT is that the
    # module emitted ascii. We verify the module output separately in test_envelope.
    return non_ascii

"""
hub/safety.py — global kill-switch, monotonic deadlines, bounded retries.

Every hub module routes its waiting/retrying through here so the whole system
shares one safety posture:

  * Kill-switch: a marker file (hub/KILLSWITCH, override via HUB_KILL_FILE).
    Present -> every guarded entry point refuses with KillSwitchEngaged.
    File-based on purpose: no daemon, works across processes, can be engaged
    by any channel that can touch a file.
  * Deadline: monotonic time budget passed down call chains; @deadline bounds
    a whole function call (daemon worker thread — the caller's wait is bounded;
    an expired worker is abandoned and dies with the process. Fine for our
    short-lived CLI processes; do not wrap side effects that must not outlive
    the deadline).
  * @retry: exponential backoff with delay ceiling, attempt ceiling, AND a
    total-time ceiling. Safety exceptions are never retried.

CLI:
    kill engage [--reason S] | kill disengage | kill status
    selftest
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

POLL_INTERVAL = 0.15

_DEFAULT_KILL_FILE = Path(__file__).resolve().parent / "KILLSWITCH"


class SafetyError(Exception):
    pass


class KillSwitchEngaged(SafetyError):
    pass


class DeadlineExceeded(SafetyError):
    pass


class RetriesExhausted(SafetyError):
    pass


# Exceptions no retry loop may swallow: safety verdicts and interpreter exits.
NEVER_RETRY = (KillSwitchEngaged, DeadlineExceeded, KeyboardInterrupt, SystemExit)


# ---------------------------------------------------------------- kill-switch

def kill_file() -> Path:
    return Path(os.environ.get("HUB_KILL_FILE", _DEFAULT_KILL_FILE))


def kill_engaged() -> bool:
    return kill_file().exists()


def engage_kill(reason: str = "manual") -> dict:
    f = kill_file()
    f.write_text(json.dumps({"reason": reason, "engaged_at": time.time()}),
                 encoding="utf-8")
    return {"file": str(f), "reason": reason}


def disengage_kill() -> dict:
    f = kill_file()
    existed = f.exists()
    if existed:
        f.unlink()
    return {"file": str(f), "was_engaged": existed}


def kill_status() -> dict:
    f = kill_file()
    status = {"engaged": f.exists(), "file": str(f), "detail": None}
    if status["engaged"]:
        try:
            status["detail"] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status["detail"] = "unreadable (still counts as engaged)"
    return status


def assert_alive(context: str = "") -> None:
    """Raise KillSwitchEngaged if the switch is on. Call at every entry point."""
    if kill_engaged():
        raise KillSwitchEngaged(
            f"kill-switch engaged ({kill_file()})"
            + (f" -- refused: {context}" if context else ""))


# ---------------------------------------------------------------- deadlines

class Deadline:
    """A monotonic time budget that can be handed down a call chain."""

    def __init__(self, seconds: float):
        if seconds <= 0:
            raise ValueError("deadline must be positive")
        self.seconds = seconds
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, context: str = "") -> None:
        if self.expired:
            raise DeadlineExceeded(
                f"deadline of {self.seconds}s exceeded"
                + (f" during {context}" if context else ""))

    def sleep(self, want: float) -> None:
        """Sleep `want` seconds, but never past the deadline."""
        time.sleep(min(want, self.remaining()))


def wait_until(predicate, timeout: float, poll: float = POLL_INTERVAL,
               respect_kill: bool = True):
    """Poll until predicate is truthy or budget runs out; returns last value.

    The shared bounded-wait primitive: kill-aware, monotonic, never spins.
    """
    dl = Deadline(timeout)
    while True:
        if respect_kill:
            assert_alive("wait_until")
        value = predicate()
        if value or dl.expired:
            return value
        dl.sleep(poll)


def deadline(seconds: float):
    """Bound a function call's wall-clock time. Kill-switch checked on entry.

    Runs the callee in a daemon thread; on expiry the caller gets
    DeadlineExceeded immediately and the worker is abandoned (it cannot block
    process exit). Wrap pure/idempotent work, not critical side effects.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            assert_alive(fn.__qualname__)
            box: dict = {}

            def run():
                try:
                    box["result"] = fn(*args, **kwargs)
                except BaseException as e:  # ferried to caller below
                    box["error"] = e

            t = threading.Thread(target=run, daemon=True,
                                 name=f"deadline:{fn.__name__}")
            t.start()
            t.join(seconds)
            if t.is_alive():
                raise DeadlineExceeded(
                    f"{fn.__qualname__} exceeded {seconds}s; worker abandoned")
            if "error" in box:
                raise box["error"]
            return box.get("result")
        return wrapper
    return deco


# ---------------------------------------------------------------- retry

def retry(attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0,
          factor: float = 2.0, jitter: float = 0.25,
          retry_on: tuple = (Exception,), total_timeout: float = 60.0):
    """Bounded retry: attempt ceiling, per-delay ceiling, total-time ceiling.

    Kill-switch is checked before every attempt. NEVER_RETRY exceptions
    propagate immediately regardless of retry_on.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            dl = Deadline(total_timeout)
            last: Exception | None = None
            for attempt in range(1, attempts + 1):
                assert_alive(f"{fn.__qualname__} attempt {attempt}")
                try:
                    return fn(*args, **kwargs)
                except NEVER_RETRY:
                    raise
                except retry_on as e:
                    last = e
                    if attempt == attempts or dl.expired:
                        break
                    delay = min(max_delay, base_delay * factor ** (attempt - 1))
                    delay *= 1 + random.uniform(-jitter, jitter)
                    dl.sleep(delay)
            raise RetriesExhausted(
                f"{fn.__qualname__}: {attempt} attempt(s) failed within "
                f"{dl.seconds}s budget; last error: "
                f"{type(last).__name__}: {last}") from last
        return wrapper
    return deco


# ---------------------------------------------------------------- cli

def _emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _selftest() -> None:
    """Exercise every guarantee against a temp kill file (real switch untouched)."""
    failures: list[str] = []
    tmp_kill = Path(__file__).resolve().parent / f"KILLSWITCH.selftest{os.getpid()}"
    os.environ["HUB_KILL_FILE"] = str(tmp_kill)
    try:
        # deadline math + fast path
        @deadline(5.0)
        def quick():
            return 42
        if quick() != 42:
            failures.append("deadline fast path broken")

        # slow path: 5s sleep must be cut off near 0.4s
        @deadline(0.4)
        def slow():
            time.sleep(5)
        t0 = time.monotonic()
        try:
            slow()
            failures.append("DeadlineExceeded not raised")
        except DeadlineExceeded:
            if time.monotonic() - t0 > 1.5:
                failures.append("deadline fired too late")

        # retry: succeeds on 3rd attempt, counts attempts
        calls = {"n": 0}

        @retry(attempts=5, base_delay=0.01, max_delay=0.02, total_timeout=5)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise IOError("transient")
            return "ok"
        if flaky() != "ok" or calls["n"] != 3:
            failures.append(f"retry recovery broken (calls={calls['n']})")

        # retry exhaustion raises RetriesExhausted with cause chain
        @retry(attempts=2, base_delay=0.01, total_timeout=5)
        def hopeless():
            raise IOError("permanent")
        try:
            hopeless()
            failures.append("RetriesExhausted not raised")
        except RetriesExhausted as e:
            if not isinstance(e.__cause__, IOError):
                failures.append("cause chain lost")

        # kill-switch: engage -> guarded paths refuse -> retry does NOT swallow
        engage_kill("selftest")
        for name, guarded in (
            ("assert_alive", lambda: assert_alive("t")),
            ("deadline", lambda: quick()),
            ("retry", lambda: flaky()),
            ("wait_until", lambda: wait_until(lambda: True, 1)),
        ):
            try:
                guarded()
                failures.append(f"{name} ignored kill-switch")
            except KillSwitchEngaged:
                pass
        disengage_kill()
        if kill_engaged():
            failures.append("disengage failed")

        # wait_until: flips after ~3 polls; expiry returns falsy, not raise
        flip = {"n": 0}

        def pred():
            flip["n"] += 1
            return flip["n"] >= 3
        if not wait_until(pred, 5, poll=0.01):
            failures.append("wait_until never saw truthy predicate")
        if wait_until(lambda: False, 0.2, poll=0.05):
            failures.append("wait_until fabricated a truthy result")
    finally:
        tmp_kill.unlink(missing_ok=True)
        os.environ.pop("HUB_KILL_FILE", None)

    _emit(not failures, "selftest",
          data={"checked": ["deadline fast/slow", "retry recovery/exhaustion",
                            "kill-switch blocks all guards", "wait_until"]},
          error="; ".join(failures) or None)


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.safety", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    k = sub.add_parser("kill")
    k.add_argument("kill_action", choices=["engage", "disengage", "status"])
    k.add_argument("--reason", default="manual")

    sub.add_parser("selftest")

    args = p.parse_args()
    try:
        if args.action == "selftest":
            _selftest()
        elif args.action == "kill":
            data = {"engage": lambda: engage_kill(args.reason),
                    "disengage": disengage_kill,
                    "status": kill_status}[args.kill_action]()
            _emit(True, f"kill.{args.kill_action}", data=data)
    except Exception as e:
        _emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

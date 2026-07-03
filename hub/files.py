"""
hub/files.py — contained file search/read/stage for the Remote Hub.

Containment model
-----------------
Allowlist territory: every path is resolved (symlinks/junctions followed)
and must land inside a permitted root — by default the user's content dirs
(Desktop, Documents, Downloads, Pictures, Videos, Music), extendable via
HUB_FILES_ROOTS (semicolon-separated). System dirs and AppData are
unreachable by construction, not by enumeration.

Inside allowed territory, secret material is still refused: sensitive
dot-directories (.ssh, .aws, ...) and key-file patterns (id_rsa*, *.pem,
*.pfx, *.kdbx, ...) never leave disk through this module.

Bounds: reads are hard-capped (never flood stdout / chat context), binaries
are refused with metadata instead, searches carry a wall-clock Deadline,
an entry-scan ceiling, a result ceiling, and mid-walk kill-switch checks.

CLI:
    roots                                     show territory + refusal rules
    search --pattern GLOB [--root R] [--limit N] [--timeout S]
    info   --path P [--hash]
    read   --path P [--max-bytes N] [--tail]
    stage  --path P [--max-mb N]              validate + metadata for sending
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import mimetypes
import os
import sys
from pathlib import Path

try:  # package import or direct script run
    from hub.safety import Deadline, DeadlineExceeded, KillSwitchEngaged, \
        SafetyError, assert_alive
except ImportError:
    from safety import Deadline, DeadlineExceeded, KillSwitchEngaged, \
        SafetyError, assert_alive

ENV_ROOTS = "HUB_FILES_ROOTS"
DEFAULT_ROOT_NAMES = ("Desktop", "Documents", "Downloads",
                      "Pictures", "Videos", "Music")

DENY_DIR_NAMES = {".ssh", ".aws", ".azure", ".gnupg", ".gpg",
                  ".kube", ".docker", "appdata"}
DENY_FILE_GLOBS = ("id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.pfx",
                   "*.p12", "*.ppk", "*.kdbx", "*.keystore", "*.jks")

READ_DEFAULT_BYTES = 65_536
READ_HARD_CAP = 262_144          # nothing bigger ever reaches stdout
BINARY_SNIFF_BYTES = 8_192
SEARCH_DEFAULT_TIMEOUT = 15.0
SEARCH_TIMEOUT_CAP = 60.0
SEARCH_RESULT_CAP = 500
SEARCH_SCAN_CAP = 100_000        # directory entries examined, max
STAGE_DEFAULT_MB = 50
HASH_DEADLINE_SEC = 30.0
KILL_CHECK_EVERY = 500           # entries between kill-switch polls


class ContainmentError(SafetyError):
    """Path is outside allowed territory or matches secret-material rules."""


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- containment

def allowed_roots() -> list[Path]:
    env = os.environ.get(ENV_ROOTS)
    if env:
        raw = [Path(r).expanduser() for r in env.split(";") if r.strip()]
    else:
        home = Path.home()
        raw = [home / name for name in DEFAULT_ROOT_NAMES]
    return [p.resolve() for p in raw if p.is_dir()]


def resolve_contained(raw: str, *, want_file: bool = False) -> Path:
    """Resolve a path and prove it is fair game, or raise ContainmentError."""
    p = Path(raw).expanduser().resolve()  # follows symlinks/junctions

    roots = allowed_roots()
    if not any(p == root or root in p.parents for root in roots):
        raise ContainmentError(
            f"{p} is outside allowed territory {[str(r) for r in roots]}")

    lowered = [part.lower() for part in p.parts]
    hits = DENY_DIR_NAMES.intersection(lowered)
    if hits:
        raise ContainmentError(
            f"{p} passes through refused directory {sorted(hits)}")

    for pattern in DENY_FILE_GLOBS:
        if fnmatch.fnmatch(p.name.lower(), pattern):
            raise ContainmentError(
                f"{p.name} matches secret-material pattern {pattern!r}; "
                "this module never moves key material")

    if not p.exists():
        raise ContainmentError(f"{p} does not exist")
    if want_file and not p.is_file():
        raise ContainmentError(f"{p} is not a regular file")
    return p


def _entry_meta(p: Path) -> dict:
    st = p.stat()
    return {"path": str(p), "size_bytes": st.st_size,
            "modified_epoch": round(st.st_mtime)}


def _sha256(p: Path) -> str:
    dl = Deadline(HASH_DEADLINE_SEC)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            dl.check(f"sha256({p.name})")
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- actions

def do_roots(args) -> None:
    emit(True, "roots", data={
        "roots": [str(r) for r in allowed_roots()],
        "env_override": ENV_ROOTS,
        "refused_dirs": sorted(DENY_DIR_NAMES),
        "refused_file_patterns": list(DENY_FILE_GLOBS),
        "read_hard_cap_bytes": READ_HARD_CAP,
    })


def do_search(args) -> None:
    roots = allowed_roots()
    if args.root:
        roots = [resolve_contained(args.root)]

    timeout = min(args.timeout, SEARCH_TIMEOUT_CAP)
    limit = min(args.limit, SEARCH_RESULT_CAP)
    dl = Deadline(timeout)
    pattern = args.pattern.lower()

    results: list[dict] = []
    scanned = 0
    bounded_by = None
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # prune refused directories in place — walk never descends there
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in DENY_DIR_NAMES]
            for name in filenames:
                scanned += 1
                if scanned % KILL_CHECK_EVERY == 0:
                    assert_alive("files.search")
                    if dl.expired:
                        bounded_by = f"timeout ({timeout}s)"
                        break
                if scanned > SEARCH_SCAN_CAP:
                    bounded_by = f"scan cap ({SEARCH_SCAN_CAP} entries)"
                    break
                if fnmatch.fnmatch(name.lower(), pattern):
                    if not any(fnmatch.fnmatch(name.lower(), g)
                               for g in DENY_FILE_GLOBS):
                        try:
                            results.append(_entry_meta(Path(dirpath) / name))
                        except OSError:
                            continue
            if bounded_by:
                break
        if bounded_by:
            break

    results.sort(key=lambda r: r["modified_epoch"], reverse=True)
    if len(results) > limit:
        bounded_by = bounded_by or f"result limit ({limit})"
        results = results[:limit]
    emit(True, "search", data={
        "pattern": args.pattern, "matches": results, "scanned": scanned,
        "complete": bounded_by is None, "bounded_by": bounded_by,
    })


def do_info(args) -> None:
    p = resolve_contained(args.path)
    data = _entry_meta(p)
    data.update({
        "is_dir": p.is_dir(),
        "mime_guess": mimetypes.guess_type(p.name)[0],
    })
    if args.hash and p.is_file():
        data["sha256"] = _sha256(p)
    emit(True, "info", data=data)


def do_read(args) -> None:
    p = resolve_contained(args.path, want_file=True)
    size = p.stat().st_size
    budget = min(args.max_bytes, READ_HARD_CAP)

    with p.open("rb") as f:
        sniff = f.read(BINARY_SNIFF_BYTES)
        if b"\x00" in sniff:
            emit(False, "read",
                 data={**_entry_meta(p),
                       "mime_guess": mimetypes.guess_type(p.name)[0]},
                 error="binary file; refusing to dump into chat context — "
                       "use `stage` + send instead")
        if args.tail:
            f.seek(max(0, size - budget))
        else:
            f.seek(0)
        raw = f.read(budget)

    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("cp1252", errors="replace"), "cp1252~replace"

    emit(True, "read", data={
        "path": str(p), "size_bytes": size, "returned_bytes": len(raw),
        "truncated": len(raw) < size, "window": "tail" if args.tail else "head",
        "encoding": encoding, "content": text,
    })


def do_stage(args) -> None:
    p = resolve_contained(args.path, want_file=True)
    size = p.stat().st_size
    cap = args.max_mb * 1024 * 1024
    if size > cap:
        emit(False, "stage", data=_entry_meta(p),
             error=f"{size / 1048576:.1f}MB exceeds --max-mb {args.max_mb}; "
                   "raise the cap explicitly if intended")
    emit(True, "stage", data={
        **_entry_meta(p),
        "mime_guess": mimetypes.guess_type(p.name)[0],
        "sha256": _sha256(p),
        "cleared_for_send": True,
    })


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="hub.files", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("roots")

    s = sub.add_parser("search")
    s.add_argument("--pattern", required=True)
    s.add_argument("--root")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--timeout", type=float, default=SEARCH_DEFAULT_TIMEOUT)

    i = sub.add_parser("info")
    i.add_argument("--path", required=True)
    i.add_argument("--hash", action="store_true")

    r = sub.add_parser("read")
    r.add_argument("--path", required=True)
    r.add_argument("--max-bytes", type=int, default=READ_DEFAULT_BYTES)
    r.add_argument("--tail", action="store_true")

    st = sub.add_parser("stage")
    st.add_argument("--path", required=True)
    st.add_argument("--max-mb", type=int, default=STAGE_DEFAULT_MB)

    args = p.parse_args()
    handler = {"roots": do_roots, "search": do_search, "info": do_info,
               "read": do_read, "stage": do_stage}[args.action]
    try:
        assert_alive(f"files.{args.action}")
        handler(args)
    except (ContainmentError, KillSwitchEngaged, DeadlineExceeded) as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

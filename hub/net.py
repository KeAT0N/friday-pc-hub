"""
hub/net.py — read-only network telemetry for the Remote Hub.

A dumb sensor like system.py: connectivity, addresses, adapters, Wi-Fi state,
and a bounded ping probe. Read-only by design — nothing here changes network
config (Wi-Fi connect/disconnect is deliberately out of scope for now).

Units explicit in key names: *_mbps, *_ms, *_percent. Every external call is
bounded (socket timeouts, subprocess timeouts, a ping count ceiling) so a
flaky link can never hang the hub.

Dependency-free: psutil + socket + netsh/ping via subprocess.

CLI:
    status                          hostname, primary IP, online probe
    adapters                        per-NIC up/speed/IPv4/IPv6/MAC
    wifi                            current SSID / signal / state (or absent)
    ping --host H [--count N]       bounded reachability + loss/latency
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys

import psutil

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

ONLINE_PROBE = ("1.1.1.1", 53)   # TCP connect to a public DNS port = online check
PROBE_TIMEOUT = 2.0
SUBPROC_TIMEOUT = 10.0
PING_COUNT_CAP = 8               # keeps worst-case ping under SUBPROC_TIMEOUT
PING_WAIT_MS = 1000              # per-reply timeout
# Safe host charset: hostnames/IPs only, no leading dash (blocks arg injection).
HOST_RE = re.compile(r"(?![-.])[A-Za-z0-9.:_-]{1,253}$")


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- sections

def _local_ip() -> str | None:
    """Primary outbound IPv4 via the UDP-connect trick (sends no packets)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _online() -> bool:
    try:
        with socket.create_connection(ONLINE_PROBE, PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def sec_status() -> dict:
    return {
        "hostname": socket.gethostname(),
        "local_ip": _local_ip(),
        "online": _online(),
        "probe": f"{ONLINE_PROBE[0]}:{ONLINE_PROBE[1]}",
    }


def sec_adapters() -> list[dict]:
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        out.append({
            "name": name,
            "is_up": bool(st.isup) if st else None,
            "speed_mbps": st.speed if st else None,
            "ipv4": [a.address for a in addrs if a.family == socket.AF_INET],
            "ipv6": [a.address for a in addrs if a.family == socket.AF_INET6],
            "mac": next((a.address for a in addrs
                         if a.family == psutil.AF_LINK), None),
        })
    return out


def sec_wifi() -> dict:
    """Current Wi-Fi interface state via netsh; fail-soft to present:false."""
    try:
        p = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                           capture_output=True, text=True, timeout=SUBPROC_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return {"present": False, "error": f"{type(e).__name__}: {e}"}
    combined = (p.stdout + p.stderr).lower()
    if "no wireless interface" in combined or "not running" in combined:
        return {"present": False}
    fields = {}
    for line in (p.stdout or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()
    if not fields.get("ssid") and not fields.get("state"):
        return {"present": False}
    return {"present": True, "ssid": fields.get("ssid"),
            "state": fields.get("state"), "signal": fields.get("signal"),
            "radio": fields.get("radio type")}


def _parse_ping(text: str) -> dict:
    """Parse the Windows ping summary. Anchored on the 'Packets:' line so it
    can't pick up 'bytes=' / 'TTL=' numbers; English-locale best-effort with an
    honest None fallback (reachability itself comes from the exit code)."""
    out = {"transmitted": None, "received": None,
           "loss_percent": None, "avg_ms": None}
    m = re.search(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+),\s*"
                  r"Lost\s*=\s*(\d+)\s*\((\d+)%", text)
    if m:
        out["transmitted"] = int(m.group(1))
        out["received"] = int(m.group(2))
        out["loss_percent"] = int(m.group(4))
    a = re.search(r"Average\s*=\s*(\d+)\s*ms", text)
    if a:
        out["avg_ms"] = int(a.group(1))
    return out


def do_ping(host: str, count: int) -> None:
    if not HOST_RE.match(host):
        emit(False, "ping", error=f"invalid host {host!r}")
    count = max(1, min(count, PING_COUNT_CAP))
    try:
        p = subprocess.run(
            ["ping", "-n", str(count), "-w", str(PING_WAIT_MS), host],
            capture_output=True, text=True, timeout=SUBPROC_TIMEOUT)
    except subprocess.TimeoutExpired:
        emit(False, "ping", error=f"ping {host} timed out")
    except OSError as e:
        emit(False, "ping", error=f"{type(e).__name__}: {e}")
    data = {"host": host, "count": count, "reachable": p.returncode == 0,
            **_parse_ping(p.stdout or "")}
    emit(True, "ping", data=data)


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="hub.net", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("status")
    sub.add_parser("adapters")
    sub.add_parser("wifi")

    pg = sub.add_parser("ping")
    pg.add_argument("--host", required=True)
    pg.add_argument("--count", type=int, default=4)

    args = p.parse_args()
    try:
        assert_alive(f"net.{args.action}")
        if args.action == "status":
            emit(True, "status", data=sec_status())
        elif args.action == "adapters":
            emit(True, "adapters", data=sec_adapters())
        elif args.action == "wifi":
            emit(True, "wifi", data=sec_wifi())
        elif args.action == "ping":
            do_ping(args.host, args.count)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

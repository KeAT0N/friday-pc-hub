"""
hub/security.py — PC security posture audit for the Remote Hub (Phase 5).

`audit` is a read-only, one-shot posture scorecard: Windows Defender, firewall
profiles, UAC, RDP, SMBv1, listening TCP ports, BitLocker, local admins, and
the guest account. A single bounded PowerShell gather assembles everything into
JSON, each check wrapped in try/catch so one missing cmdlet or an unelevated
run yields null for that field instead of sinking the whole report.

Most checks work unelevated; a few (BitLocker, sometimes local admins) need
admin and come back null with a note. Mutating hardening verbs (scan,
firewall-on, ...) will land later — dry-run-by-default + --confirm + admin-gated,
and they must NEVER weaken a protection.

`concerns` flags only hard on/off security invariants (Defender off, a firewall
profile off, UAC off, RDP on, SMBv1 on, guest on) — universally-agreed issues,
NOT tunable thresholds. Numeric values (signature age, port counts) are reported
raw; the orchestrator decides what's "too old". Predicate logic stays in the brain.

Dependency-free: PowerShell via subprocess + stdlib json.

CLI:
    audit                    read-only posture scorecard (checks + concerns)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

PS_TIMEOUT = 40.0
LISTEN_SAMPLE = 40   # cap the listening-port list so a busy box can't flood

# One PowerShell pass; every check is individually try/catch'd -> null on failure.
AUDIT_SCRIPT = r'''
$ErrorActionPreference="SilentlyContinue"
$r=[ordered]@{}
$r.elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
try{$d=Get-MpComputerStatus;$r.defender=@{realtime=[bool]$d.RealTimeProtectionEnabled;antivirus=[bool]$d.AntivirusEnabled;antispyware=[bool]$d.AntispywareEnabled;tamper=[bool]$d.IsTamperProtected;signature_age_days=[int]$d.AntivirusSignatureAge;quick_scan_age_days=[int]$d.QuickScanAge}}catch{$r.defender=$null}
try{$fw=Get-NetFirewallProfile;$h=[ordered]@{};foreach($p in $fw){$h[[string]$p.Name]=[bool]$p.Enabled};$r.firewall=$h}catch{$r.firewall=$null}
try{$r.uac_enabled=[bool]((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name EnableLUA).EnableLUA)}catch{$r.uac_enabled=$null}
try{$r.rdp_enabled=(((Get-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections).fDenyTSConnections) -eq 0)}catch{$r.rdp_enabled=$null}
try{$r.smb1_enabled=[bool](Get-SmbServerConfiguration).EnableSMB1Protocol}catch{$r.smb1_enabled=$null}
try{$bl=Get-BitLockerVolume;$h=[ordered]@{};foreach($v in $bl){$h[[string]$v.MountPoint]=[string]$v.ProtectionStatus};$r.bitlocker=$h}catch{$r.bitlocker=$null}
try{$lp=Get-NetTCPConnection -State Listen|Sort-Object LocalPort -Unique;$ports=@($lp|ForEach-Object{@{port=[int]$_.LocalPort;address=[string]$_.LocalAddress;pid=[int]$_.OwningProcess}});$r.listening=@{count=@($ports).Count;ports=@($ports)}}catch{$r.listening=$null}
try{$adm=Get-LocalGroupMember -Group "Administrators";$r.local_admins=@($adm|ForEach-Object{[string]$_.Name})}catch{$r.local_admins=$null}
try{$r.guest_enabled=[bool](Get-LocalUser -Name "Guest").Enabled}catch{$r.guest_enabled=$null}
$r|ConvertTo-Json -Depth 6 -Compress
'''


# Recent-signal report. Reading the Security log (4625) needs admin; every check
# is try/catch'd so an unelevated run reports available:false + a note, not error.
INTRUDERS_TEMPLATE = r'''
$ErrorActionPreference="Stop"
$hours=__HOURS__; $max=__MAX__
$r=[ordered]@{}
$r.elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
try{
  $since=(Get-Date).AddHours(-$hours)
  $ev=Get-WinEvent -FilterHashtable @{LogName="Security";Id=4625;StartTime=$since} -MaxEvents $max
  $list=@($ev|ForEach-Object{@{time=$_.TimeCreated.ToString("o");account=[string]$_.Properties[5].Value;ip=[string]$_.Properties[19].Value}})
  $r.failed_logons=@{available=$true;window_hours=$hours;count=@($list).Count;events=@($list)}
}catch{
  if($_.Exception.Message -match "No events were found"){$r.failed_logons=@{available=$true;window_hours=$hours;count=0;events=@()}}
  else{$r.failed_logons=@{available=$false;error=[string]$_.Exception.Message}}
}
try{
  $td=Get-MpThreatDetection|Sort-Object InitialDetectionTime -Descending|Select-Object -First $max
  $tl=@($td|ForEach-Object{@{time=[string]$_.InitialDetectionTime;threat_id=[string]$_.ThreatID;cleaned=[bool]$_.ActionSuccess}})
  $r.defender_detections=@{available=$true;count=@($tl).Count;items=@($tl)}
}catch{$r.defender_detections=@{available=$false;error=[string]$_.Exception.Message}}
try{$adm=Get-LocalGroupMember -Group "Administrators";$r.local_admins=@($adm|ForEach-Object{[string]$_.Name})}catch{$r.local_admins=$null}
$r|ConvertTo-Json -Depth 6 -Compress
'''


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _gather() -> dict:
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-Command", AUDIT_SCRIPT],
        capture_output=True, text=True, timeout=PS_TIMEOUT)
    text = (p.stdout or "").strip()
    if not text:
        raise RuntimeError(
            f"powershell produced no output (stderr: {p.stderr.strip()[:200]})")
    return json.loads(text)


def concerns(a: dict) -> list[str]:
    """Hard on/off security invariants only — no tunable thresholds."""
    out = []
    d = a.get("defender")
    if isinstance(d, dict):
        if d.get("realtime") is False:
            out.append("Defender real-time protection is OFF")
        if d.get("antivirus") is False:
            out.append("Defender antivirus is OFF")
        if d.get("tamper") is False:
            out.append("Defender tamper protection is OFF")
    fw = a.get("firewall")
    if isinstance(fw, dict):
        for name, enabled in fw.items():
            if enabled is False:
                out.append(f"Firewall is OFF on the {name} profile")
    if a.get("uac_enabled") is False:
        out.append("UAC is disabled")
    if a.get("rdp_enabled") is True:
        out.append("Remote Desktop (RDP) is enabled")
    if a.get("smb1_enabled") is True:
        out.append("SMBv1 is enabled (legacy, insecure)")
    if a.get("guest_enabled") is True:
        out.append("Guest account is enabled")
    return out


def _notes(a: dict) -> list[str]:
    notes = []
    if not a.get("elevated"):
        null_checks = [k for k in ("bitlocker", "local_admins")
                       if a.get(k) is None]
        if null_checks:
            notes.append(f"not elevated; {', '.join(null_checks)} unavailable "
                         "(run in an admin terminal for full coverage)")
    if isinstance(a.get("listening"), dict):
        ports = a["listening"].get("ports") or []
        if len(ports) > LISTEN_SAMPLE:
            a["listening"]["ports"] = ports[:LISTEN_SAMPLE]
            notes.append(f"listening ports truncated to {LISTEN_SAMPLE}")
    return notes


def do_audit(args) -> None:
    raw = _gather()
    notes = _notes(raw)
    emit(True, "audit", data={**raw, "concerns": concerns(raw), "notes": notes})


# ---------------------------------------------------------------- intruders (RO)

INTRUDERS_HOURS_DEFAULT = 24
INTRUDERS_HOURS_CAP = 168          # one week
INTRUDERS_MAX_DEFAULT = 50
INTRUDERS_MAX_CAP = 200


def _run_intruders(hours: int, max_events: int) -> dict:
    hours = max(1, min(hours, INTRUDERS_HOURS_CAP))
    max_events = max(1, min(max_events, INTRUDERS_MAX_CAP))
    script = (INTRUDERS_TEMPLATE
              .replace("__HOURS__", str(hours))
              .replace("__MAX__", str(max_events)))
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=PS_TIMEOUT)
    text = (p.stdout or "").strip()
    if not text:
        raise RuntimeError(
            f"powershell produced no output (stderr: {p.stderr.strip()[:200]})")
    return json.loads(text)


def intruder_concerns(a: dict) -> list[str]:
    """Hard signals only. Count-based thresholds (e.g. 'many 4625') live in the
    brain / the 5b tripwire's data profile, never here."""
    out = []
    dd = a.get("defender_detections")
    if isinstance(dd, dict) and dd.get("available") and dd.get("count", 0) > 0:
        out.append(f"Defender recorded {dd['count']} recent threat detection(s)")
    return out


def _intruder_notes(a: dict) -> list[str]:
    notes = []
    fl = a.get("failed_logons")
    if isinstance(fl, dict) and not fl.get("available"):
        notes.append("reading failed logons (Security event log) needs an "
                     "elevated terminal; run the audit as admin for this signal")
    return notes


def do_intruders(args) -> None:
    raw = _run_intruders(args.hours, args.max)
    emit(True, "intruders", data={**raw, "concerns": intruder_concerns(raw),
                                  "notes": _intruder_notes(raw)})


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.security", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("audit")

    it = sub.add_parser("intruders")
    it.add_argument("--hours", type=int, default=INTRUDERS_HOURS_DEFAULT,
                    help=f"failed-logon look-back window (1-{INTRUDERS_HOURS_CAP})")
    it.add_argument("--max", type=int, default=INTRUDERS_MAX_DEFAULT,
                    help=f"max events per signal (1-{INTRUDERS_MAX_CAP})")

    args = p.parse_args()
    try:
        assert_alive(f"security.{args.action}")
        if args.action == "audit":
            do_audit(args)
        elif args.action == "intruders":
            do_intruders(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except subprocess.TimeoutExpired:
        emit(False, args.action, error=f"{args.action} timed out after {PS_TIMEOUT}s")
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

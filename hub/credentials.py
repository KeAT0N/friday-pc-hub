"""
hub/credentials.py — secure credential access layer for the Remote Hub.

Security model
--------------
Secrets are consumed IN-PROCESS by hub modules (`provider.get(...).reveal()`)
and never cross a process boundary. The CLI here can only answer existence
and availability questions — there is intentionally no code path that writes
a secret value to stdout, stderr, logs, or JSON.

Backends are selected by name (or HUB_CRED_PROVIDER env var). Only the
NullProvider placeholder is enabled today; the env-var and Windows Credential
Manager (keyring) backends are wired into the registry but raise
ProviderUnavailable until green-lit.

CLI:
    providers                                   backend registry + availability
    check --service S [--account A] [--provider P]   existence only, never values
    selftest                                    prove Secret cannot leak
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from abc import ABC, abstractmethod

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

REDACTED = "Secret(<redacted>)"
DEFAULT_ACCOUNT = "default"
ENV_SELECTOR = "HUB_CRED_PROVIDER"


class CredentialError(Exception):
    """Base class; messages must never contain secret values."""


class CredentialNotFound(CredentialError):
    pass


class ProviderUnavailable(CredentialError):
    pass


# ---------------------------------------------------------------- secret

class Secret:
    """An opaque wrapper that refuses to disclose its value implicitly.

    repr(), str(), format(), logging interpolation, and json.dumps() all
    yield the redaction marker or raise. The only disclosure path is an
    explicit .reveal() call by consuming code.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not value:
            raise TypeError("Secret requires a non-empty str")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    # -- immutability ------------------------------------------------
    def __setattr__(self, name, value):
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name):
        raise AttributeError("Secret is immutable")

    # -- every implicit disclosure path is sealed ---------------------
    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec) -> str:
        return REDACTED

    def __reduce__(self):
        raise TypeError("Secret cannot be pickled or copied")

    def __eq__(self, other) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    __hash__ = None  # unhashable: keeps Secrets out of dict keys / sets

    def __bool__(self) -> bool:
        return True

    def __len__(self):
        # Even length is signal; don't provide it.
        raise TypeError("Secret does not expose its length")


# ---------------------------------------------------------------- interface

class CredentialProvider(ABC):
    """Contract every backend implements.

    `service` is the logical system ("github", "smtp"); `account` the identity
    within it. Read-only backends inherit the default set/delete, which refuse.
    """

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Can this backend actually serve requests on this machine, now?"""

    @abstractmethod
    def get(self, service: str, account: str = DEFAULT_ACCOUNT) -> Secret:
        """Return the secret or raise CredentialNotFound. Never returns str."""

    @abstractmethod
    def exists(self, service: str, account: str = DEFAULT_ACCOUNT) -> bool:
        ...

    def set(self, service: str, account: str, secret: Secret) -> None:
        raise ProviderUnavailable(f"{self.name!r} backend is read-only")

    def delete(self, service: str, account: str = DEFAULT_ACCOUNT) -> None:
        raise ProviderUnavailable(f"{self.name!r} backend is read-only")


# ---------------------------------------------------------------- backends

class NullProvider(CredentialProvider):
    """Active placeholder: holds nothing, refuses everything, explains itself."""

    name = "null"

    @classmethod
    def available(cls) -> bool:
        return True

    def get(self, service: str, account: str = DEFAULT_ACCOUNT) -> Secret:
        raise CredentialNotFound(
            f"no credential for service={service!r} account={account!r}: "
            "NullProvider is active. Enable a real backend "
            "(HUB_CRED_PROVIDER=keyring|env) once implemented."
        )

    def exists(self, service: str, account: str = DEFAULT_ACCOUNT) -> bool:
        return False


class EnvProvider(CredentialProvider):
    """NOT YET ENABLED. Contract for the env-var backend.

    Will read HUB_CRED_{SERVICE}__{ACCOUNT} (upper-cased, non-alnum -> _)
    from the process environment. Read-only by nature.
    """

    name = "env"

    @staticmethod
    def var_name(service: str, account: str) -> str:
        clean = lambda s: "".join(c if c.isalnum() else "_" for c in s.upper())
        return f"HUB_CRED_{clean(service)}__{clean(account)}"

    @classmethod
    def available(cls) -> bool:
        return False  # flip when green-lit

    def get(self, service: str, account: str = DEFAULT_ACCOUNT) -> Secret:
        raise ProviderUnavailable("env backend not yet enabled")

    def exists(self, service: str, account: str = DEFAULT_ACCOUNT) -> bool:
        raise ProviderUnavailable("env backend not yet enabled")


class KeyringProvider(CredentialProvider):
    """NOT YET ENABLED. Contract for Windows Credential Manager via `keyring`.

    Will map (service, account) -> keyring.get_password(f"hub:{service}", account).
    Secrets stay DPAPI-encrypted at rest, scoped to the Windows user. This is
    the intended production backend; supports set/delete for enrollment.
    """

    name = "keyring"

    @classmethod
    def available(cls) -> bool:
        return False  # flip when green-lit (requires `pip install keyring`)

    def get(self, service: str, account: str = DEFAULT_ACCOUNT) -> Secret:
        raise ProviderUnavailable("keyring backend not yet enabled")

    def exists(self, service: str, account: str = DEFAULT_ACCOUNT) -> bool:
        raise ProviderUnavailable("keyring backend not yet enabled")


# ---------------------------------------------------------------- registry

_PROVIDERS: dict[str, type[CredentialProvider]] = {
    p.name: p for p in (NullProvider, EnvProvider, KeyringProvider)
}


def get_provider(name: str | None = None) -> CredentialProvider:
    """Resolve a backend by name, arg > HUB_CRED_PROVIDER env > null."""
    name = name or os.environ.get(ENV_SELECTOR) or "null"
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ProviderUnavailable(
            f"unknown provider {name!r}; known: {sorted(_PROVIDERS)}")
    if not cls.available():
        raise ProviderUnavailable(f"provider {name!r} is not enabled/available")
    return cls()


# ---------------------------------------------------------------- cli
# Existence and health checks only. No subcommand returns secret material.

def _emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _selftest() -> None:
    """Prove the Secret wrapper cannot leak through implicit channels."""
    canary = "canary-3f9a1c"
    s = Secret(canary)
    leaks = {
        "repr": repr(s), "str": str(s), "format": f"{s}",
        "log_style": "value=%s" % s,
    }
    failures = [k for k, v in leaks.items() if canary in v]
    for attempt, expect in (
        (lambda: json.dumps({"s": s}), TypeError),
        (lambda: s.__reduce__(), TypeError),
        (lambda: len(s), TypeError),
        (lambda: setattr(s, "_value", "x"), AttributeError),
    ):
        try:
            attempt()
            failures.append(f"{expect.__name__} not raised")
        except expect:
            pass
    if s.reveal() != canary:
        failures.append("reveal() broken")
    if Secret(canary) != Secret(canary) or Secret(canary) == Secret("other"):
        failures.append("constant-time eq broken")
    _emit(not failures, "selftest",
          data={"channels_checked": [*leaks, "json", "pickle", "len", "mutation"]},
          error="; ".join(failures) or None)


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.credentials", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("providers")
    sub.add_parser("selftest")

    c = sub.add_parser("check")
    c.add_argument("--service", required=True)
    c.add_argument("--account", default=DEFAULT_ACCOUNT)
    c.add_argument("--provider")

    args = p.parse_args()
    try:
        assert_alive(f"credentials.{args.action}")
        if args.action == "providers":
            _emit(True, "providers", data=[
                {"name": cls.name, "available": cls.available(),
                 "writable": cls.set is not CredentialProvider.set}
                for cls in _PROVIDERS.values()
            ])
        elif args.action == "selftest":
            _selftest()
        elif args.action == "check":
            provider = get_provider(args.provider)
            _emit(True, "check", data={
                "provider": provider.name, "service": args.service,
                "account": args.account,
                "exists": provider.exists(args.service, args.account),
            })
    except (CredentialError, KillSwitchEngaged) as e:
        _emit(False, args.action, error=str(e))
    except Exception as e:  # never let a traceback (or its locals) hit stdout
        _emit(False, args.action, error=f"unhandled {type(e).__name__}")


if __name__ == "__main__":
    main()

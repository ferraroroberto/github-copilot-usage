"""Opt-in access from other devices on the machine's own Tailscale network.

Off, the server answers on 127.0.0.1 only. On, ``resolve`` asks the local ``tailscale`` CLI for this
machine's tailnet IPv4 address and name and for a certificate, so the dashboard can also be served over
HTTPS on that one address. It never produces ``0.0.0.0`` or any address outside Tailscale's own range,
and every missing piece (no CLI, not signed in, HTTPS not enabled for the tailnet, no certificate)
comes back as a reason with no address, so the caller stays on loopback and says why.

The name and address are read at runtime and never stored: nothing here names a machine or a tailnet.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import config
from src.runtime_data import runtime_data_dir
from src.usage_store import APP

_log = logging.getLogger(__name__)

CERT_FILENAME = "tailnet.crt"
KEY_FILENAME = "tailnet.key"

# The CLI answers from the local daemon in well under a second; a certificate that has to be fetched
# from the coordination server is the slow call, hence the longer bound.
_QUERY_TIMEOUT_SECS = 10
_CERT_TIMEOUT_SECS = 60

# Tailscale hands out IPv4 addresses from the carrier-grade-NAT block only.
_TAILNET_RANGE = ipaddress.ip_network("100.64.0.0/10")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Where the CLI lives when it is not on PATH (the GUI installers do not always add it).
_CLI_FALLBACKS = (
    ("ProgramFiles", "Tailscale", "tailscale.exe"),
    ("ProgramFiles(x86)", "Tailscale", "tailscale.exe"),
)
_MAC_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


@dataclass(frozen=True)
class Plan:
    """What the tailnet bind will be: ``live`` when it has an address, a name and a certificate."""

    enabled: bool
    host: Optional[str] = None
    name: Optional[str] = None
    cert_file: Optional[Path] = None
    key_file: Optional[Path] = None
    # Why an enabled option is not live; None when it is live or off.
    reason: Optional[str] = None

    @property
    def live(self) -> bool:
        return bool(self.enabled and self.host and self.name and self.cert_file and self.key_file)

    def url(self, port: int) -> Optional[str]:
        return "https://%s:%d/" % (self.name, port) if self.live else None


def enabled() -> bool:
    """Whether the option is switched on in config.json; off unless it says true."""
    return config.load().get("tailnet_access") is True


def find_cli() -> Optional[str]:
    """The ``tailscale`` executable, or None when this machine has none."""
    found = shutil.which("tailscale")
    if found:
        return found
    candidates: List[Path] = []
    for env, *parts in _CLI_FALLBACKS:
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base, *parts))
    candidates.append(Path(_MAC_CLI))
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _run(cli: str, args: List[str], timeout: int) -> Tuple[int, str, str]:
    """One CLI call; a hang or a missing binary is a non-zero result with its own message."""
    try:
        done = subprocess.run(
            [cli] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "tailscale %s did not answer within %d s" % (args[0], timeout)
    except OSError as exc:
        return 127, "", "tailscale could not be started: %s" % exc
    return done.returncode, done.stdout, done.stderr


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no message"


def tailnet_address(status: Dict[str, Any]) -> Optional[str]:
    """This machine's tailnet IPv4 address from a ``status --json`` document, or None."""
    me = status.get("Self") or {}
    for raw in me.get("TailscaleIPs") or []:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.version == 4 and ip in _TAILNET_RANGE:
            return str(ip)
    return None


def _status(cli: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    code, out, err = _run(cli, ["status", "--json"], _QUERY_TIMEOUT_SECS)
    if code != 0:
        return None, "tailscale status failed: " + _first_line(err or out)
    try:
        doc = json.loads(out)
    except ValueError:
        return None, "tailscale status returned unreadable output"
    if not isinstance(doc, dict):
        return None, "tailscale status returned unreadable output"
    return doc, None


def cert_paths() -> Tuple[Path, Path]:
    """Where the certificate and its key are kept: this app's data folder, never the repo."""
    folder = runtime_data_dir(APP, create=True)
    return folder / CERT_FILENAME, folder / KEY_FILENAME


def fetch_cert(cli: str, name: str) -> Tuple[Optional[Tuple[Path, Path]], Optional[str]]:
    """Ask Tailscale for the certificate, which renews it when it is close to expiry."""
    try:
        cert_file, key_file = cert_paths()
    except OSError as exc:
        return None, "the data folder for the certificate is not writable: %s" % exc
    code, out, err = _run(
        cli, ["cert", "--cert-file", str(cert_file), "--key-file", str(key_file), name], _CERT_TIMEOUT_SECS)
    if code != 0:
        return None, "tailscale cert failed: " + _first_line(err or out)
    return (cert_file, key_file), None


def cert_digest(plan: Plan) -> str:
    """A fingerprint of the certificate on disk, so a renewal can be told from an unchanged one."""
    if plan.cert_file is None:
        return ""
    try:
        return hashlib.sha256(plan.cert_file.read_bytes()).hexdigest()
    except OSError:
        return ""


def resolve() -> Plan:
    """The tailnet bind for this moment; never raises, and never returns an address it cannot serve."""
    if not enabled():
        return Plan(enabled=False)
    cli = find_cli()
    if cli is None:
        return Plan(enabled=True, reason="the tailscale command was not found on this machine")
    status, problem = _status(cli)
    if status is None:
        return Plan(enabled=True, reason=problem)
    if status.get("BackendState") != "Running":
        return Plan(enabled=True, reason="Tailscale is not connected (state: %s)" % status.get("BackendState"))
    host = tailnet_address(status)
    if host is None:
        return Plan(enabled=True, reason="this machine has no Tailscale IPv4 address")
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    domains = [str(d) for d in status.get("CertDomains") or []]
    if not name or name not in domains:
        return Plan(enabled=True, reason=(
            "HTTPS certificates are not enabled for this tailnet "
            "(Tailscale admin console, DNS, enable MagicDNS and HTTPS Certificates)"))
    fetched, problem = fetch_cert(cli, name)
    if fetched is None:
        return Plan(enabled=True, reason=problem)
    return Plan(enabled=True, host=host, name=name, cert_file=fetched[0], key_file=fetched[1])


def renew(plan: Plan) -> Optional[str]:
    """Re-run the certificate request for a live plan; returns the failure reason, None on success."""
    cli = find_cli()
    if cli is None or not plan.name:
        return "the tailscale command was not found on this machine"
    _, problem = fetch_cert(cli, plan.name)
    return problem


# The plan the running server is acting on, for Settings. Set once by whoever starts the listeners.
_serving: Plan = Plan(enabled=False)
_serving_port = 0


def set_serving(plan: Plan, port: int) -> None:
    global _serving, _serving_port
    _serving, _serving_port = plan, port
    if plan.live:
        _log.info("ℹ️ tailnet access on: https://%s (bound to the Tailscale address only)", plan.name)
    elif plan.enabled:
        _log.warning("⚠️ tailnet access is on but not live, staying on loopback: %s", plan.reason)


def describe() -> Dict[str, Any]:
    """The Settings view: the switch, whether this process serves the tailnet, its URL, why not."""
    now = enabled()
    return {
        "enabled": now,
        "live": _serving.live,
        "url": _serving.url(_serving_port),
        "reason": _serving.reason if now and _serving.enabled else None,
        # The bind is chosen at start, so a switch flipped since then is not in effect yet.
        "restart_needed": now != _serving.enabled,
    }

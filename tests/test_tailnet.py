"""The opt-in Tailscale access: off by default, a live plan only when every piece is there, a reason (never a
crash, never a wider bind) when one is missing, and the settings API and listeners that carry it.

The names, addresses and certificate here are made up; the real ones are read at runtime."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from starlette.testclient import TestClient

from app.listeners import Listeners
from src import aggregate, config, refresh, tailnet

HOST = "100.64.0.7"
NAME = "box.tailnet.invalid"


def _status(**over: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "BackendState": "Running",
        "Self": {"DNSName": NAME + ".", "TailscaleIPs": ["fd7a:115c:a1e0::1", HOST]},
        "CertDomains": [NAME],
    }
    doc.update(over)
    return doc


class FakeCli:
    """Stands in for the tailscale command: a status document and the outcome of `cert`."""

    def __init__(self, tmp_path: Path, status: Optional[Dict[str, Any]] = None, cert_code: int = 0) -> None:
        self.status = _status() if status is None else status
        self.cert_code = cert_code
        self.calls: List[List[str]] = []
        self.tmp_path = tmp_path

    def __call__(self, cli: str, args: List[str], timeout: int) -> Tuple[int, str, str]:
        self.calls.append(args)
        if args[0] == "status":
            return 0, json.dumps(self.status), ""
        if self.cert_code:
            return self.cert_code, "", "no certificate for this node\nsecond line"
        Path(args[args.index("--cert-file") + 1]).write_text("cert", encoding="utf-8")
        Path(args[args.index("--key-file") + 1]).write_text("key", encoding="utf-8")
        return 0, "", ""


@pytest.fixture
def on(settings_file, monkeypatch, tmp_path):
    """Tailnet access switched on, the CLI present, and the certificate folder inside tmp_path."""
    settings_file.write_text(json.dumps({"tailnet_access": True}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_USAGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(tailnet, "find_cli", lambda: "tailscale")
    return tmp_path


def test_off_by_default_without_touching_tailscale(settings_file, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the tailscale command must not run while the option is off")

    monkeypatch.setattr(tailnet, "_run", boom)
    monkeypatch.setattr(tailnet, "find_cli", boom)
    assert config.load()["tailnet_access"] is False
    plan = tailnet.resolve()
    assert plan.enabled is False and plan.live is False and plan.host is None


def test_only_a_boolean_true_turns_it_on(settings_file):
    settings_file.write_text(json.dumps({"tailnet_access": "yes"}), encoding="utf-8")
    assert tailnet.enabled() is False


def test_live_plan_binds_the_tailnet_address_with_its_certificate(on, monkeypatch):
    fake = FakeCli(on)
    monkeypatch.setattr(tailnet, "_run", fake)
    plan = tailnet.resolve()
    assert plan.live and plan.host == HOST and plan.name == NAME
    assert plan.url(8377) == "https://%s:8377/" % NAME
    assert plan.cert_file is not None and plan.cert_file.read_text(encoding="utf-8") == "cert"
    assert on / "data" in plan.cert_file.parents  # the certificate never lands in the repo


@pytest.mark.parametrize("addresses", [[], ["fd7a:115c:a1e0::1"], ["0.0.0.0"], ["10.0.0.5"], ["127.0.0.1"], ["not-an-ip"]])
def test_an_address_outside_tailscales_range_is_never_bound(addresses):
    assert tailnet.tailnet_address({"Self": {"TailscaleIPs": addresses}}) is None


@pytest.mark.parametrize("status, reason", [
    (_status(BackendState="Stopped"), "not connected"),
    (_status(Self={"DNSName": NAME + ".", "TailscaleIPs": []}), "no Tailscale IPv4"),
    (_status(CertDomains=[]), "HTTPS certificates are not enabled"),
    (_status(Self={"DNSName": "", "TailscaleIPs": [HOST]}), "HTTPS certificates are not enabled"),
])
def test_a_missing_piece_falls_back_with_a_reason(on, monkeypatch, status, reason):
    monkeypatch.setattr(tailnet, "_run", FakeCli(on, status))
    plan = tailnet.resolve()
    assert plan.enabled and not plan.live and plan.host is None
    assert reason in str(plan.reason)


def test_no_tailscale_command_falls_back_with_a_reason(on, monkeypatch):
    monkeypatch.setattr(tailnet, "find_cli", lambda: None)
    plan = tailnet.resolve()
    assert not plan.live and "tailscale command was not found" in str(plan.reason)


def test_a_failed_certificate_request_falls_back_with_its_first_line(on, monkeypatch):
    monkeypatch.setattr(tailnet, "_run", FakeCli(on, cert_code=1))
    plan = tailnet.resolve()
    assert not plan.live and plan.host is None
    assert plan.reason == "tailscale cert failed: no certificate for this node"


def test_an_unreadable_status_falls_back(on, monkeypatch):
    monkeypatch.setattr(tailnet, "_run", lambda cli, args, timeout: (0, "not json", ""))
    assert "unreadable" in str(tailnet.resolve().reason)


@pytest.fixture
def client(monkeypatch, settings_file):
    monkeypatch.setattr(aggregate, "gather_records", lambda: ([], []))
    monkeypatch.setattr(refresh, "store", refresh.SnapshotStore())
    monkeypatch.setattr(tailnet, "_serving", tailnet.Plan(enabled=False))
    from app.server import app

    with TestClient(app) as c:
        yield c


def test_api_reports_off_then_saves_the_switch_for_the_next_start(client, settings_file):
    assert client.get("/api/tailnet").json() == {
        "enabled": False, "live": False, "url": None, "reason": None, "restart_needed": False}
    saved = client.put("/api/tailnet", json={"enabled": True}).json()
    assert saved["enabled"] is True and saved["live"] is False and saved["restart_needed"] is True
    assert json.loads(settings_file.read_text(encoding="utf-8"))["tailnet_access"] is True
    assert client.put("/api/tailnet", json={"enabled": "yes"}).status_code == 400


def test_api_shows_the_live_url_or_the_reason(client):
    config.save({"tailnet_access": True})
    plan = tailnet.Plan(enabled=True, host=HOST, name=NAME, cert_file=Path("c"), key_file=Path("k"))
    tailnet.set_serving(plan, 8377)
    live = client.get("/api/tailnet").json()
    assert live["live"] is True and live["url"] == "https://%s:8377/" % NAME and live["restart_needed"] is False

    tailnet.set_serving(tailnet.Plan(enabled=True, reason="Tailscale is not connected (state: Stopped)"), 8377)
    down = client.get("/api/tailnet").json()
    assert down["live"] is False and down["url"] is None and "not connected" in down["reason"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str) -> httpx.Response:
    deadline = time.time() + 10
    while True:
        try:
            return httpx.get(url, timeout=2)
        except httpx.HTTPError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def test_off_serves_loopback_only(settings_file, monkeypatch):
    monkeypatch.setattr(aggregate, "gather_records", lambda: ([], []))
    listeners = Listeners(_free_port())
    listeners.start()
    try:
        assert _wait_for(listeners.loopback_url + "api/version").status_code == 200
        names = [s.getsockname() for srv in listeners._loopback.servers for s in srv.sockets]
        assert names == [("127.0.0.1", listeners.port)]
        assert listeners._tailnet is None and listeners.tailnet_url is None
    finally:
        listeners.stop()


def test_a_tailnet_address_that_cannot_bind_leaves_loopback_serving(settings_file, monkeypatch, tmp_path):
    monkeypatch.setattr(aggregate, "gather_records", lambda: ([], []))
    # 203.0.113.0/24 is documentation space: no interface owns it, so the bind fails like a dropped tailnet.
    unbindable = tailnet.Plan(enabled=True, host="203.0.113.9", name=NAME, cert_file=tmp_path / "c", key_file=tmp_path / "k")
    monkeypatch.setattr(tailnet, "resolve", lambda: unbindable)
    listeners = Listeners(_free_port())
    listeners.start()
    try:
        assert _wait_for(listeners.loopback_url + "api/version").status_code == 200
        deadline = time.time() + 5
        while listeners.plan.live and time.time() < deadline:
            time.sleep(0.05)
        assert listeners.plan.live is False and "could not be bound" in str(listeners.plan.reason)
        assert tailnet.describe()["live"] is False
        assert _wait_for(listeners.loopback_url + "health").status_code == 200
    finally:
        listeners.stop()

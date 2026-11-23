"""The sockets the dashboard answers on: always loopback over HTTP, plus the tailnet over HTTPS when on.

Both servers run on one event loop in one thread, because the app keeps loop-bound state (the shared
refresh future, the billing client) that two loops would each half own. Only the loopback server runs the
app's startup and shutdown hooks. The tailnet socket is bound here, not by uvicorn, so a failed bind
becomes a logged fallback to loopback rather than uvicorn's process exit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import sys
import threading
from typing import Callable, List, Optional, Tuple

import uvicorn

from src import pools, tailnet

_log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
APP_IMPORT = "app.server:app"

# The certificate is requested again this often; Tailscale only renews it once it is near expiry, and a
# renewed file is picked up by rebuilding the tailnet server.
_RENEW_CHECK_SECS = 24 * 3600
# An enabled option that is not live yet (Tailscale still starting at login) is tried again this often.
_RETRY_SECS = 60
# How long stop() waits for the serving thread; a stated guess for an idle local server.
_STOP_JOIN_SECS = 5


def _tailnet_socket(host: str, port: int) -> socket.socket:
    """A listening socket on the tailnet address only; raises OSError when it cannot bind."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":  # on Windows SO_REUSEADDR would let another process share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


async def _serve(server: uvicorn.Server, sockets: Optional[List[socket.socket]], label: str) -> None:
    """Run one server to its end; a listener that cannot start is logged and never ends the other."""
    try:
        await server.serve(sockets=sockets)
    except SystemExit:  # uvicorn exits the process when it cannot start; here it must only end this listener
        _log.error("❌ the %s listener could not start", label)


class Listeners:
    """Start, renew and stop every socket the dashboard serves on."""

    def __init__(self, port: int, log_level: str = "warning") -> None:
        self.port = port
        self.log_level = log_level
        self.plan = tailnet.Plan(enabled=False)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loopback: Optional[uvicorn.Server] = None
        self._tailnet: Optional[uvicorn.Server] = None
        self._stopping = False
        self._stop_event: Optional[asyncio.Event] = None
        # Called, from the serving thread, whenever the tailnet address appears or is given up, so a tray
        # menu that shows it can redraw.
        self.on_tailnet_change: Optional[Callable[[], None]] = None

    @property
    def loopback_url(self) -> str:
        return "http://%s:%d/" % (LOOPBACK, self.port)

    @property
    def tailnet_url(self) -> Optional[str]:
        return self.plan.url(self.port)

    def _notify_tailnet_change(self) -> None:
        callback = self.on_tailnet_change
        if callback is None:
            return
        try:
            callback()
        except Exception:  # a menu that cannot redraw must never end the listener that called it
            _log.warning("⚠️ tailnet access: the change callback failed", exc_info=True)

    def start(self) -> None:
        self.plan = tailnet.resolve()
        tailnet.set_serving(self.plan, self.port)
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="listeners", daemon=True)
        self._thread.start()

    def _signal_stop(self) -> None:
        self._stopping = True
        for server in (self._loopback, self._tailnet):
            if server is not None:
                server.should_exit = True
        if self._stop_event is not None:
            self._stop_event.set()

    def stop(self) -> None:
        self._stopping = True
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._signal_stop)
            except RuntimeError:  # the loop closed between the check and the call: it is already stopping
                pass
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_SECS)

    def wait(self) -> None:
        """Block until the listeners end, waking often enough for Ctrl-C to be heard."""
        while self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._loopback = uvicorn.Server(
            uvicorn.Config(APP_IMPORT, host=LOOPBACK, port=self.port, log_level=self.log_level))
        tasks = [asyncio.ensure_future(_serve(self._loopback, None, "loopback"))]
        if self.plan.enabled:
            tasks.append(asyncio.ensure_future(self._tailnet_task()))
        await tasks[0]
        # The loopback server is the dashboard: when it ends, nothing else keeps the process serving.
        self._signal_stop()
        await asyncio.gather(*tasks[1:])

    async def _sleep_or_stop(self, secs: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def _tailnet_task(self) -> None:
        """Wait until the tailnet bind can be made, then serve it; the loopback server never waits on this."""
        loop = asyncio.get_running_loop()
        while not self._stopping and not self.plan.live:
            await self._sleep_or_stop(_RETRY_SECS)
            if self._stopping:
                return
            plan = await loop.run_in_executor(pools.TAILNET, tailnet.resolve)
            if plan.live:
                self.plan = plan
                tailnet.set_serving(plan, self.port)
                self._notify_tailnet_change()
        if self.plan.live and not self._stopping:
            await self._tailnet_loop()

    def _build_tailnet_server(self) -> Optional[Tuple[uvicorn.Server, socket.socket]]:
        try:
            sock = _tailnet_socket(str(self.plan.host), self.port)
        except OSError as exc:
            reason = "the Tailscale address could not be bound: %s" % exc
            self.plan = dataclasses.replace(self.plan, host=None, reason=reason)
            tailnet.set_serving(self.plan, self.port)
            self._notify_tailnet_change()
            return None
        # lifespan is off so the app's startup and shutdown hooks run once, on the loopback server.
        config = uvicorn.Config(
            APP_IMPORT, log_level=self.log_level, lifespan="off",
            ssl_certfile=str(self.plan.cert_file), ssl_keyfile=str(self.plan.key_file))
        return uvicorn.Server(config), sock

    async def _tailnet_loop(self) -> None:
        """Serve the tailnet socket, rebuilding the server when the certificate is renewed."""
        assert self._stop_event is not None
        digest = tailnet.cert_digest(self.plan)
        while not self._stopping:
            built = self._build_tailnet_server()
            if built is None:
                return
            server, sock = built
            self._tailnet = server
            task = asyncio.ensure_future(_serve(server, [sock], "tailnet"))
            renewed = await self._wait_for_renewal(task, digest)
            server.should_exit = True
            await task
            if not renewed:
                return
            digest = tailnet.cert_digest(self.plan)

    async def _wait_for_renewal(self, task: "asyncio.Future[None]", digest: str) -> bool:
        """Wait out the renewal interval; True when a renewed certificate means the server must be rebuilt."""
        assert self._stop_event is not None
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            while not self._stopping:
                done, _ = await asyncio.wait({task, stop}, timeout=_RENEW_CHECK_SECS,
                                             return_when=asyncio.FIRST_COMPLETED)
                if done:
                    return False
                problem = await asyncio.get_running_loop().run_in_executor(pools.TAILNET, tailnet.renew, self.plan)
                if problem:
                    _log.warning("⚠️ tailnet access: certificate renewal failed, keeping the current one: %s", problem)
                elif tailnet.cert_digest(self.plan) != digest:
                    _log.info("ℹ️ tailnet access: certificate renewed, restarting the tailnet listener")
                    return True
            return False
        finally:
            stop.cancel()

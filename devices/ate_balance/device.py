"""AteBalanceDevice — sockets + threads driver for the ATE balance.

Mirrors the callback/lifecycle shape of ``wtdaq.devices.daqbook2000.DAQbook2000``
(``on_frame`` / ``on_status`` callbacks, ``connect``/``start``/``stop``,
``frame_count()``) so it can later be merged into the wtdaq framework and fed
through its SyncManager unchanged.

Connection model (Operations Manual section 6)
----------------------------------------------
* **TMSC control (TCP)** - the OGI *dials* us, so by default this device is the
  TCP **server** (``connect_mode="listen"``).  A ``"dial"`` mode is provided as
  a fallback for setups where the client must initiate.
* **TMSD data (UDP)** - we bind and receive the continuous LOADS stream.
* **OGIT trigger (UDP)** - we *send* ``TMS_CONNECT`` to prompt the OGI to dial.

When ``config.force_sim`` is set, no sockets are opened; an in-process
:class:`~ate_balance.emulator.OgiSimCore` generates the load stream and answers
commands, so the GUI is fully exercisable with no hardware or emulator.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import protocol as P
from .config import AteConfig, CONNECT_LISTEN, SPAN_FULL, SPAN_HALF
from .datamodel import BalanceFrame
from .emulator import OgiSimCore

log = logging.getLogger(__name__)

SERIAL_MAX = 32767  # Manual: serial numbers range 0..32767


class AteBalanceDevice:
    """Threaded client for the ATE external balance."""

    def __init__(self, config: Optional[AteConfig] = None):
        self.config = config or AteConfig()

        # User callbacks (invoked from IO threads — marshal to GUI thread).
        self.on_frame: Optional[Callable[[BalanceFrame], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_reply: Optional[Callable[[P.ParsedMessage], None]] = None

        self._connected = False
        self._running = False
        self._sim = False
        self._frame_count = 0
        self._last_had_sync = False

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._t0 = 0.0

        # sockets
        self._udp_sock: Optional[socket.socket] = None
        self._srv_sock: Optional[socket.socket] = None
        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_lock = threading.Lock()
        self._link = threading.Event()    # TMSC control link established

        # serial counter
        self._serial = 0
        self._serial_lock = threading.Lock()

        # simulation core
        self._core: Optional[OgiSimCore] = None

    # ── public state ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._running

    @property
    def sim_mode(self) -> bool:
        return self._sim

    @property
    def link_up(self) -> bool:
        """True once the TMSC control link is established (always True in sim)."""
        return self._sim or self._link.is_set()

    @property
    def last_had_sync(self) -> bool:
        return self._last_had_sync

    def frame_count(self) -> int:
        return self._frame_count

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._stop.clear()
        self._frame_count = 0
        self._t0 = time.perf_counter()

        if self.config.force_sim:
            self._sim = True
            self._core = OgiSimCore()
            self._link.set()
            self._spawn(self._sim_loop, "ate-sim")
            self._connected = True
            self._status("Simulation mode — synthetic balance data")
            return

        self._sim = False
        # Bind UDP data receiver first (surfaces port conflicts synchronously).
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.bind((self.config.bind_host, self.config.tmsd_port))
        self._udp_sock.settimeout(0.3)

        if self.config.connect_mode == CONNECT_LISTEN:
            self._srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._srv_sock.bind((self.config.bind_host, self.config.tmsc_port))
            self._srv_sock.listen(1)
            self._srv_sock.settimeout(0.3)
            self._spawn(self._accept_loop, "ate-accept")
        else:
            self._spawn(self._dial_loop, "ate-dial")

        self._spawn(self._udp_loop, "ate-udp")
        self._connected = True
        self._status(f"Connected ({self.config.connect_mode}); "
                     f"TMSC:{self.config.tmsc_port} TMSD:{self.config.tmsd_port}")

        if self.config.auto_trigger:
            self.trigger_connect()

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._running = False
        self._stop.set()
        self._link.clear()
        with self._ctrl_lock:
            _safe_close(self._ctrl_sock)
            self._ctrl_sock = None
        _safe_close(self._srv_sock)
        _safe_close(self._udp_sock)
        self._srv_sock = None
        self._udp_sock = None
        for th in self._threads:
            th.join(timeout=1.5)
        self._threads = []
        self._connected = False
        self._sim = False
        self._status("Disconnected")

    def start(self) -> None:
        """Begin emitting/recording frames (the OGI streams continuously)."""
        if not self._connected:
            raise RuntimeError("connect() before start()")
        self._running = True
        self._status("Acquiring")

    def stop(self) -> None:
        self._running = False
        self._status("Idle")

    # ── trigger ──────────────────────────────────────────────────────────
    def trigger_connect(self) -> None:
        """Send TMS_CONNECT to the OGI so it (re)dials this client."""
        if self._sim:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(P.TRIGGER_CONNECT.encode("ascii"),
                     (self.config.ogi_ip, self.config.ogit_port))
            s.close()
            self._status(f"Sent TMS_CONNECT to {self.config.ogi_ip}:"
                         f"{self.config.ogit_port}")
        except OSError as exc:
            self._status(f"Trigger failed: {exc}")

    # ── commands (TMS -> OGI) ────────────────────────────────────────────
    def zero(self) -> int:
        return self._send(P.CMD_ZERO)

    def take_sample(self, seconds: Optional[float] = None) -> int:
        secs = self.config.default_sample_seconds if seconds is None else seconds
        return self._send(P.CMD_TAKE_SAMPLE, int(round(secs)))

    def lock(self) -> int:
        return self._send(P.CMD_LOCK)

    def unlock(self) -> int:
        return self._send(P.CMD_UNLOCK)

    def get_lock_status(self) -> int:
        return self._send(P.CMD_GET_LOCK_STATUS)

    def get_positions(self) -> int:
        return self._send(P.CMD_GET_POSITIONS)

    def goto_yaw(self, degrees: float) -> int:
        return self._send(P.CMD_GOTO_YAW, float(degrees))

    def goto_inc(self, degrees: float) -> int:
        return self._send(P.CMD_GOTO_INC, float(degrees))

    def get_filters(self) -> int:
        return self._send(P.CMD_GET_FILTERS)

    # ── span-config alpha/beta mapping ───────────────────────────────────
    # The driver OWNS the model-span mapping: hosts (Freestream adapter,
    # panels) command LOGICAL alpha/beta through goto_alpha/goto_beta and
    # the driver resolves which PHYSICAL drive moves:
    #   full (default): alpha → incidence drive, beta → yaw drive.
    #   half (½-span model on the turntable): alpha → YAW drive (with the
    #        yaw drive's limits); beta is REJECTED; the incidence drive is
    #        never commanded.
    # The raw goto_inc/goto_yaw primitives above stay untouched for manual
    # per-drive control.
    @property
    def span_config(self) -> str:
        """Normalized model-span configuration ("full" | "half")."""
        span = getattr(self.config, "span_config", SPAN_FULL)
        return SPAN_HALF if span == SPAN_HALF else SPAN_FULL

    @property
    def half_span(self) -> bool:
        return self.span_config == SPAN_HALF

    def alpha_limits(self) -> Tuple[float, float]:
        """Soft limits for the LOGICAL alpha axis (deg)."""
        return P.YAW_LIMITS_DEG if self.half_span else P.INC_LIMITS_DEG

    def beta_limits(self) -> Optional[Tuple[float, float]]:
        """Soft limits for the LOGICAL beta axis; None = no beta (½-span)."""
        return None if self.half_span else P.YAW_LIMITS_DEG

    def goto_alpha(self, degrees: float) -> int:
        """Command the logical alpha axis (drive per span_config)."""
        dest = float(degrees)
        lo, hi = self.alpha_limits()
        if not (lo <= dest <= hi):
            raise ValueError(
                f"alpha target {dest:+.2f}° outside limits "
                f"[{lo:+.1f}, {hi:+.1f}]"
                + (" (yaw drive, ½-span)" if self.half_span else ""))
        return self.goto_yaw(dest) if self.half_span else self.goto_inc(dest)

    def goto_beta(self, degrees: float) -> int:
        """Command the logical beta axis (yaw drive; rejected in ½-span)."""
        if self.half_span:
            raise ValueError(
                "beta rejected: the ½-span configuration has no beta axis "
                "(alpha is the yaw drive; the incidence drive is unused)")
        dest = float(degrees)
        lo, hi = P.YAW_LIMITS_DEG
        if not (lo <= dest <= hi):
            raise ValueError(f"beta target {dest:+.2f}° outside limits "
                             f"[{lo:+.1f}, {hi:+.1f}]")
        return self.goto_yaw(dest)

    def map_positions(self, yaw_deg: float, inc_deg: float
                      ) -> Dict[str, float]:
        """Physical drive positions → logical axes per span_config.

        full: {"alpha": inc, "beta": yaw}; half: {"alpha": yaw} (the
        incidence position is not a model axis in ½-span)."""
        if self.half_span:
            return {"alpha": float(yaw_deg)}
        return {"alpha": float(inc_deg), "beta": float(yaw_deg)}

    def logical_axis_for_reply(self, command: str) -> Optional[str]:
        """Which logical axis a YAW_*/INC_* motion reply refers to.

        None = the reply belongs to a drive that is not a logical axis in
        the current span configuration (the unused incidence drive in
        ½-span)."""
        if command in (P.RSP_YAW_MOVING, P.RSP_YAW_COMPLETE):
            return "alpha" if self.half_span else "beta"
        if command in (P.RSP_INC_MOVING, P.RSP_INC_COMPLETE):
            return None if self.half_span else "alpha"
        return None

    def stop_all_motion(self) -> int:
        return self._send(P.CMD_STOP_ALL)

    def kill_yi_drives(self) -> int:
        return self._send(P.CMD_KILL_YI)

    # ── command plumbing ─────────────────────────────────────────────────
    def _next_serial(self) -> int:
        with self._serial_lock:
            self._serial = (self._serial + 1) % (SERIAL_MAX + 1)
            return self._serial

    def _send(self, command: str, *params) -> int:
        serial = self._next_serial()
        if self._sim:
            self._sim_respond(serial, command, params)
            return serial
        data = P.build_command(serial, command, *params)
        with self._ctrl_lock:
            sock = self._ctrl_sock
            if sock is None:
                self._status(f"Control link not up — '{command}' not sent")
                return serial
            try:
                sock.sendall(data)
            except OSError as exc:
                self._status(f"Send failed: {exc}")
        return serial

    def _sim_respond(self, serial: int, command: str, params) -> None:
        assert self._core is not None
        msg = P.ParsedMessage(key=P.KEY_MESSAGE, serial=serial,
                              command=command.upper(),
                              params=[P._fmt_param(p) for p in params])
        t = time.perf_counter() - self._t0
        for delay, content in self._core.handle(msg, t):
            if delay <= 0:
                self._deliver_reply(serial, content)
            else:
                threading.Timer(delay, self._deliver_reply,
                                args=(serial, content)).start()

    def _deliver_reply(self, serial: int, content: str) -> None:
        if self.on_reply:
            parsed = P.parse_message(f"{P.KEY_REPLY}{serial}:{content}")
            if parsed:
                self.on_reply(parsed)

    # ── threads ──────────────────────────────────────────────────────────
    def _spawn(self, target, name) -> None:
        th = threading.Thread(target=target, name=name, daemon=True)
        th.start()
        self._threads.append(th)

    def _udp_loop(self) -> None:
        sock = self._udp_sock
        while not self._stop.is_set() and sock is not None:
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            pkt = P.decode_loads(data)
            if pkt is None:
                continue
            self._last_had_sync = pkt.had_sync
            self._emit_frame(pkt.values, pkt.sync)

    def _accept_loop(self) -> None:
        srv = self._srv_sock
        while not self._stop.is_set() and srv is not None:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.3)
            with self._ctrl_lock:
                _safe_close(self._ctrl_sock)
                self._ctrl_sock = conn
            self._link.set()
            self._status(f"OGI control connected from {addr[0]}")
            self._read_control(conn)
            self._link.clear()
            self._status("OGI control disconnected")

    def _dial_loop(self) -> None:
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect((self.config.ogi_ip, self.config.tmsc_port))
            except OSError:
                sock.close()
                self._stop.wait(0.5)
                continue
            sock.settimeout(0.3)
            with self._ctrl_lock:
                self._ctrl_sock = sock
            self._link.set()
            self._status(f"Dialled OGI {self.config.ogi_ip}:{self.config.tmsc_port}")
            self._read_control(sock)
            self._link.clear()
            with self._ctrl_lock:
                _safe_close(self._ctrl_sock)
                self._ctrl_sock = None
            self._status("Control link dropped")

    def _read_control(self, sock: socket.socket) -> None:
        buf = ""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode("ascii", "ignore")
            msgs, buf = P.extract_messages(buf)
            for raw in msgs:
                parsed = P.parse_message(raw)
                if parsed and parsed.key == P.KEY_REPLY and self.on_reply:
                    self.on_reply(parsed)

    def _sim_loop(self) -> None:
        assert self._core is not None
        period = 1.0 / 50.0
        while not self._stop.is_set():
            t = time.perf_counter() - self._t0
            vals, sync = self._core.next_loads(t)
            self._last_had_sync = True
            self._emit_frame(vals, sync)
            self._stop.wait(period)

    def _emit_frame(self, values, sync: int) -> None:
        if not self._running:
            return
        bf = BalanceFrame(timestamp=time.time(),
                          loads=P.loads_to_named(values), sync=int(sync))
        self._frame_count += 1
        if self.on_frame:
            self.on_frame(bf)

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)


def _safe_close(sock: Optional[socket.socket]) -> None:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass

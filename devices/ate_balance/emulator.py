"""Pure-Python stand-in for the balance's OGI control PC.

Two pieces:

* :class:`OgiSimCore` - the *logic* of the OGI side: synthetic load generation
  and command handling.  No sockets, fully testable; also reused by the device
  driver's built-in simulation mode so behaviour is identical online/offline.

* :class:`FakeOGI` - wraps :class:`OgiSimCore` in the real socket behaviour of
  the OGI: it **dials** the TMS control port (as the real OGI does), streams
  LOADS datagrams over UDP, and listens for ``TMS_CONNECT`` triggers.  Use it
  to integration-test the client when ``OGI_Sim.exe`` is not available::

      python -m ate_balance.emulator --tms-ip 127.0.0.1
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

from . import protocol as P

log = logging.getLogger(__name__)

# (delay_seconds, reply_content) pairs returned by OgiSimCore.handle()
Reply = Tuple[float, str]


class OgiSimCore:
    """Synthetic OGI behaviour: makes up loads and answers TMSC commands.

    Thread-safety: ``handle`` and ``next_loads`` may be called from different
    threads; a lock guards the small mutable state.
    """

    def __init__(self, seed: int = 12345):
        self._rng = np.random.default_rng(seed)
        self._lock = threading.RLock()   # reentrant: ZERO holds it then calls _raw_loads
        self.yaw_pos = 0.0       # deg
        self.inc_pos = 0.0       # deg
        self.locked = False
        self._tare = np.zeros(6)  # wire order L,P,D,S,Y,R
        self.sample_delay = 0.1  # sim TAKE_SAMPLE turnaround (s)
        self.move_time = 0.5     # sim slew time (s)

    # ── synthetic loads ──────────────────────────────────────────────────
    def _raw_loads(self, t: float) -> np.ndarray:
        """Underlying (pre-tare) wire-order loads, in N / N.m."""
        with self._lock:
            inc, yaw = self.inc_pos, self.yaw_pos
        w = 2.0 * np.pi * 0.2 * t       # slow 0.2 Hz "breathing"
        noise = self._rng.normal(0.0, 0.4, 6)
        # wire order: Lift, Pitch, Drag, Side, Yaw, Roll
        return np.array([
            200.0 + 12.0 * inc + 6.0 * np.sin(w),        # Lift  (N)
            15.0 + 0.8 * inc + 1.5 * np.sin(w + 0.5),    # Pitch (N.m)
            45.0 + 3.0 * abs(inc) + 2.0 * np.sin(w),     # Drag  (N)
            4.0 * np.sin(np.radians(yaw)) + 1.0,         # Side  (N)
            0.4 * yaw + 0.5 * np.sin(w),                 # Yaw   (N.m)
            0.6 * np.sin(w + 1.0),                       # Roll  (N.m)
        ]) + noise

    def next_loads(self, t: float) -> Tuple[List[float], int]:
        """Tared wire-order loads + sync flag for one TMSD datagram."""
        vals = self._raw_loads(t) - self._tare
        return [float(v) for v in vals], 1

    # ── command handling ─────────────────────────────────────────────────
    def handle(self, msg: P.ParsedMessage, t: float) -> List[Reply]:
        """Return a list of (delay, reply_content) for a parsed TMSC command."""
        cmd = msg.command
        p = msg.params

        if cmd == P.CMD_ZERO:
            with self._lock:
                self._tare = self._raw_loads(t)
                tare = self._tare
            return [(0.0, f"{P.RSP_TARES} " + _fmt6(tare))]

        if cmd == P.CMD_TAKE_SAMPLE:
            if not p:
                return [(0.0, "ERROR MISSING PARAMETER FOR TAKE_SAMPLE")]
            try:
                secs = float(p[0])
            except ValueError:
                return [(0.0, f"ERROR INVALID PARAMETER ({p[0]}) FOR TAKE_SAMPLE")]
            vals, _ = self.next_loads(t)
            delay = min(max(secs, 0.0) * 0.0 + self.sample_delay, 2.0)
            return [(delay, f"{P.RSP_SAMPLES} " + _fmt6(vals))]

        if cmd == P.CMD_LOCK:
            with self._lock:
                self.locked = True
            return [(self.move_time, P.RSP_BAL_LOCKED)]

        if cmd == P.CMD_UNLOCK:
            with self._lock:
                self.locked = False
            return [(self.move_time, P.RSP_BAL_UNLOCKED)]

        if cmd == P.CMD_GET_LOCK_STATUS:
            state = "LOCKED" if self.locked else "UNLOCKED"
            return [(0.0, f"{P.RSP_LOCK_STATUS} {state}")]

        if cmd == P.CMD_GET_POSITIONS:
            with self._lock:
                return [(0.0, f"{P.RSP_POSITIONS} {self.yaw_pos:6.2f} {self.inc_pos:6.2f}")]

        if cmd == P.CMD_GOTO_YAW:
            return self._goto(p, P.YAW_LIMITS_DEG, "yaw",
                              P.RSP_YAW_MOVING, P.RSP_YAW_COMPLETE)

        if cmd == P.CMD_GOTO_INC:
            return self._goto(p, P.INC_LIMITS_DEG, "inc",
                              P.RSP_INC_MOVING, P.RSP_INC_COMPLETE)

        if cmd == P.CMD_GET_FILTERS:
            per = " ".join(["300,1"] * 6)
            return [(0.0, f"{P.RSP_FILTERS} {per}")]

        if cmd == P.CMD_STOP_ALL:
            return [(0.0, "ACK_STOP_ALL")]

        if cmd == P.CMD_KILL_YI:
            return [(0.0, "ACK_KILL_YI")]

        return [(0.0, f'ERROR UNRECOGNISED COMMAND "{cmd}"')]

    def _goto(self, params, limits, axis, moving_kw, complete_kw) -> List[Reply]:
        if not params:
            return [(0.0, f"ERROR MISSING PARAMETER FOR GOTO_{axis.upper()}")]
        try:
            dest = float(params[0])
        except ValueError:
            return [(0.0, f"ERROR INVALID PARAMETER ({params[0]})")]
        lo, hi = limits
        if dest < lo or dest > hi:
            return [(0.0, f"ERROR PARAMETER ({params[0]}) EXCEEDS RANGE")]
        with self._lock:
            if axis == "yaw":
                self.yaw_pos = dest
            else:
                self.inc_pos = dest
        return [(0.0, moving_kw), (self.move_time, f"{complete_kw} {dest:6.2f}")]


def _fmt6(vals) -> str:
    return " ".join(f"{float(v):.2f}" for v in vals)


# ═════════════════════════════════════════════════════════════════════════
#  FakeOGI — real-socket OGI stand-in
# ═════════════════════════════════════════════════════════════════════════

class FakeOGI:
    """OGI stand-in over real sockets (dials the TMS, streams UDP, hears triggers)."""

    def __init__(self, tms_ip: str = "127.0.0.1",
                 tmsc_port: int = P.DEFAULT_TMSC_PORT,
                 tmsd_port: int = P.DEFAULT_TMSD_PORT,
                 ogit_port: int = P.DEFAULT_OGIT_PORT,
                 data_rate_hz: float = 50.0,
                 with_sync: bool = True,
                 core: Optional[OgiSimCore] = None):
        self.tms_ip = tms_ip
        self.tmsc_port = tmsc_port
        self.tmsd_port = tmsd_port
        self.ogit_port = ogit_port
        self.data_rate_hz = data_rate_hz
        self.with_sync = with_sync
        self.core = core or OgiSimCore()

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_lock = threading.Lock()
        self._link = threading.Event()
        self._t0 = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop.clear()
        self._t0 = time.perf_counter()
        self._spawn(self._trigger_loop, "FakeOGI-trigger")
        self._spawn(self._control_loop, "FakeOGI-control")
        self._spawn(self._data_loop, "FakeOGI-data")
        log.info("FakeOGI started -> TMS %s (ctrl %d, data %d, trig %d)",
                 self.tms_ip, self.tmsc_port, self.tmsd_port, self.ogit_port)

    def stop(self) -> None:
        self._stop.set()
        self._link.clear()
        with self._ctrl_lock:
            _safe_close(self._ctrl_sock)
            self._ctrl_sock = None
        for th in self._threads:
            th.join(timeout=1.5)
        self._threads = []

    def _spawn(self, target, name) -> None:
        th = threading.Thread(target=target, name=name, daemon=True)
        th.start()
        self._threads.append(th)

    # ── trigger: learn the TMS IP from inbound TMS_CONNECT ───────────────
    def _trigger_loop(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.ogit_port))
        except OSError as exc:
            log.warning("FakeOGI trigger bind failed: %s", exc)
            s.close()
            return
        s.settimeout(0.3)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.decode("ascii", "ignore").strip().upper() == P.TRIGGER_CONNECT:
                self.tms_ip = addr[0]
                log.info("FakeOGI got TMS_CONNECT from %s", addr[0])
                self._link.clear()   # force control loop to (re)dial
        s.close()

    # ── control: dial the TMS, answer commands ───────────────────────────
    def _control_loop(self) -> None:
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect((self.tms_ip, self.tmsc_port))
            except OSError:
                sock.close()
                self._stop.wait(0.5)
                continue
            with self._ctrl_lock:
                self._ctrl_sock = sock
            self._link.set()
            log.info("FakeOGI control linked to %s:%d", self.tms_ip, self.tmsc_port)
            buf = ""
            sock.settimeout(0.3)
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
                    self._dispatch(raw)
            self._link.clear()
            with self._ctrl_lock:
                _safe_close(self._ctrl_sock)
                self._ctrl_sock = None
            log.info("FakeOGI control link dropped")

    def _dispatch(self, raw: str) -> None:
        msg = P.parse_message(raw)
        if msg is None or msg.key != P.KEY_MESSAGE:
            return
        t = time.perf_counter() - self._t0
        for delay, content in self.core.handle(msg, t):
            if delay <= 0:
                self._send_reply(msg.serial, content)
            else:
                threading.Timer(delay, self._send_reply,
                                args=(msg.serial, content)).start()

    def _send_reply(self, serial: int, content: str) -> None:
        with self._ctrl_lock:
            sock = self._ctrl_sock
            if sock is None:
                return
            try:
                sock.sendall(P.build_reply(serial, content))
            except OSError:
                pass

    # ── data: stream LOADS to the TMS while linked ───────────────────────
    def _data_loop(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        period = 1.0 / max(self.data_rate_hz, 1.0)
        while not self._stop.is_set():
            if not self._link.wait(timeout=0.3):
                continue
            t = time.perf_counter() - self._t0
            vals, sync = self.core.next_loads(t)
            pkt = P.encode_loads(vals, sync, with_sync=self.with_sync)
            try:
                s.sendto(pkt, (self.tms_ip, self.tmsd_port))
            except OSError:
                pass
            self._stop.wait(period)
        s.close()


def _safe_close(sock: Optional[socket.socket]) -> None:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ATE OGI emulator (FakeOGI)")
    ap.add_argument("--tms-ip", default="127.0.0.1",
                    help="TMS (client) IP to dial and stream to")
    ap.add_argument("--tmsc-port", type=int, default=P.DEFAULT_TMSC_PORT)
    ap.add_argument("--tmsd-port", type=int, default=P.DEFAULT_TMSD_PORT)
    ap.add_argument("--ogit-port", type=int, default=P.DEFAULT_OGIT_PORT)
    ap.add_argument("--rate", type=float, default=50.0, help="data rate (Hz)")
    ap.add_argument("--no-sync", action="store_true",
                    help="omit the trailing int32 sync word (29-byte packets)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ogi = FakeOGI(tms_ip=args.tms_ip, tmsc_port=args.tmsc_port,
                  tmsd_port=args.tmsd_port, ogit_port=args.ogit_port,
                  data_rate_hz=args.rate, with_sync=not args.no_sync)
    ogi.start()
    print(f"FakeOGI running -> {args.tms_ip}  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        ogi.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

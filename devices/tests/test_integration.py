"""Integration tests: AteBalanceDevice <-> FakeOGI over real localhost sockets,
plus the no-socket simulation mode.

Uses offset ports (13040-13042) so a run never collides with the real rig or
another session on the default 3040-3042.

Run directly (``python tests/test_integration.py``) or via pytest.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance import protocol as P
from ate_balance.config import AteConfig
from ate_balance.device import AteBalanceDevice
from ate_balance.emulator import FakeOGI

TMSC, TMSD, OGIT = 13040, 13041, 13042


def _make_pair(with_sync=True):
    cfg = AteConfig(ogi_ip="127.0.0.1", tmsc_port=TMSC, tmsd_port=TMSD,
                    ogit_port=OGIT, auto_trigger=False)
    dev = AteBalanceDevice(cfg)
    ogi = FakeOGI(tms_ip="127.0.0.1", tmsc_port=TMSC, tmsd_port=TMSD,
                  ogit_port=OGIT, data_rate_hz=100.0, with_sync=with_sync)
    return dev, ogi


def _wait(predicate, timeout=5.0, interval=0.02):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_frames_and_commands_over_sockets():
    dev, ogi = _make_pair(with_sync=True)
    frames = []
    replies = []
    dev.on_frame = frames.append
    dev.on_reply = replies.append
    try:
        dev.connect()
        dev.start()
        ogi.start()
        assert _wait(lambda: dev.link_up), "OGI never dialled the client"
        assert _wait(lambda: len(frames) >= 20), "no LOADS stream received"
        assert dev.last_had_sync is True

        f = frames[0]
        assert set(f.loads) == set(P.WIRE_AXES)

        # Read-only command round-trip
        serial = dev.get_positions()
        assert _wait(lambda: any(r.serial == serial and
                                 r.command == P.RSP_POSITIONS
                                 for r in replies)), "no POSITIONS reply"
        pos = next(r for r in replies if r.serial == serial)
        assert len(pos.float_params()) == 2

        # Motion round-trip against the emulator (safe: emulator only)
        replies.clear()
        serial = dev.goto_yaw(15.0)
        assert _wait(lambda: any(r.serial == serial and
                                 r.command == P.RSP_YAW_COMPLETE
                                 for r in replies), timeout=6.0), \
            "no YAW_COMPLETE reply"
    finally:
        ogi.stop()
        dev.disconnect()


def test_no_sync_packets_accepted():
    dev, ogi = _make_pair(with_sync=False)
    frames = []
    dev.on_frame = frames.append
    try:
        dev.connect()
        dev.start()
        ogi.start()
        assert _wait(lambda: len(frames) >= 5), "no 29-byte LOADS received"
        assert dev.last_had_sync is False
    finally:
        ogi.stop()
        dev.disconnect()


def test_trigger_prompts_redial():
    dev, ogi = _make_pair()
    try:
        dev.connect()
        dev.start()
        ogi.start()
        assert _wait(lambda: dev.link_up)
        dev.trigger_connect()          # exercises the OGIT path
        time.sleep(0.5)
        assert dev.link_up             # link survives / re-establishes
    finally:
        ogi.stop()
        dev.disconnect()


def test_sim_mode_no_sockets():
    cfg = AteConfig(force_sim=True)
    dev = AteBalanceDevice(cfg)
    frames = []
    replies = []
    dev.on_frame = frames.append
    dev.on_reply = replies.append
    try:
        dev.connect()
        dev.start()
        assert dev.sim_mode and dev.link_up
        assert _wait(lambda: len(frames) >= 10), "sim produced no frames"
        serial = dev.zero()
        assert _wait(lambda: any(r.serial == serial and
                                 r.command == P.RSP_TARES for r in replies))
    finally:
        dev.disconnect()


def test_disconnect_is_clean():
    dev, ogi = _make_pair()
    try:
        dev.connect()
        dev.start()
        ogi.start()
        assert _wait(lambda: dev.link_up)
    finally:
        ogi.stop()
        dev.disconnect()
    assert not dev.connected
    # ports must be free again immediately
    dev2, ogi2 = _make_pair()
    try:
        dev2.connect()
        assert dev2.connected
    finally:
        ogi2.stop()
        dev2.disconnect()


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} integration tests passed.")


if __name__ == "__main__":
    _run_all()

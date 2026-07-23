"""Park-on-disconnect and position persistence (no brake — safety)."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lswt_sting.config import StingConfig
from lswt_sting.device import StingDrive


def _cfg(tmp_path, **kw):
    kw.setdefault("state_path", str(tmp_path / "state.json"))
    return StingConfig(force_sim=True, poll_ms=50, init_reset=False, **kw)


def _wait(pred, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_park_on_disconnect_is_default():
    assert StingConfig().park_on_disconnect is True
    assert StingConfig().restore_position is True


def test_beta_positive_move_sends_positive_steps(tmp_path):
    """Field-verified 2026-07-22: beta was backwards with the alpha
    sign; +1° beta must load POSITIVE steps on the wire."""
    import lswt_sting.device as sting_device

    class _Recorder(sting_device.SimSerial):
        def __init__(self, config=None):
            super().__init__(config)
            self.sent = []

        def write(self, data):
            self.sent.append(data.rstrip(b"\r\n").decode("ascii"))
            return super().write(data)

    created = []
    orig = sting_device.SimSerial
    sting_device.SimSerial = lambda c: created.append(_Recorder(c)) or \
        created[-1]
    dev = StingDrive(_cfg(tmp_path, park_on_disconnect=False,
                          restore_position=False))
    try:
        dev.connect()
        dev.set_current_angle("beta", 0.0)
        dev.set_current_angle("alpha", 0.0)
        n0 = len(created[0].sent)
        dev.move_to(beta=1.0, alpha=1.0)
        tail = created[0].sent[n0:]
        assert "2D67" in tail, tail          # beta: +1° → +67 steps
        assert "1D-2741" in tail, tail       # alpha: +1° → -2741 steps
        dev.stop_all()
    finally:
        dev.disconnect()
        sting_device.SimSerial = orig


class _WireRecorder:
    """SimSerial subclass factory that records every command line."""

    @staticmethod
    def install(monkeypatch):
        import lswt_sting.device as sting_device
        created = []

        class _Rec(sting_device.SimSerial):
            def __init__(self, config=None):
                super().__init__(config)
                self.sent = []
                created.append(self)

            def write(self, data):
                self.sent.append(data.rstrip(b"\r\n").decode("ascii"))
                return super().write(data)

        monkeypatch.setattr(sting_device, "SimSerial", _Rec)
        return created


def test_brake_output_configured_at_connect(tmp_path, monkeypatch):
    """Alpha brake on O3: connect must send OUT3B (Moving/Not-Moving
    output — SX manual ch.4) so the DRIVE releases/engages the brake
    with motion. Beta has no brake → no OUT command for unit 2."""
    created = _WireRecorder.install(monkeypatch)
    dev = StingDrive(_cfg(tmp_path, park_on_disconnect=False,
                          restore_position=False))
    try:
        dev.connect()
        sent = created[0].sent
        assert "1OUT3B" in sent, sent
        assert not any(s.startswith("2OUT") for s in sent), sent
        # configured with the motion parameters, before the final FSD1
        assert sent.index("1V.108") < sent.index("1OUT3B") \
            < sent.index("FSD1")
    finally:
        dev.disconnect()


def test_brake_output_disabled_sends_nothing(tmp_path, monkeypatch):
    created = _WireRecorder.install(monkeypatch)
    cfg = _cfg(tmp_path, park_on_disconnect=False,
               restore_position=False)
    cfg.alpha.brake_output = 0
    dev = StingDrive(cfg)
    try:
        dev.connect()
        assert not any("OUT" in s for s in created[0].sent)
    finally:
        dev.disconnect()


def test_brake_output_config_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.alpha.brake_output == 3      # default: alpha brake on O3
    assert cfg.beta.brake_output == 0
    p = tmp_path / "cfg.json"
    cfg.alpha.brake_output = 4
    cfg.save(p)
    assert StingConfig.load(p).alpha.brake_output == 4


def test_park_runs_on_disconnect(tmp_path):
    cfg = _cfg(tmp_path, park_on_disconnect=True,
               restore_position=False, park_alpha_deg=0.3)
    dev = StingDrive(cfg)
    dev.connect()
    dev.set_current_angle("alpha", 0.0)
    dev.disconnect()                          # parks at +0.3° (blocking)
    assert not dev.connected
    assert dev.state()["Alpha"]["angle"] == pytest.approx(0.3, abs=0.05)


def test_position_persists_and_restores(tmp_path):
    """Session 1 zeroes and moves; session 2 reconnects and knows the
    angle again without re-zeroing."""
    cfg = _cfg(tmp_path, park_on_disconnect=False, restore_position=True)
    dev = StingDrive(cfg)
    dev.connect()
    dev.set_current_angle("beta", 2.0)
    dev.move_to(beta=3.0)
    assert _wait(lambda: not dev.moving)
    dev.disconnect()

    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["clean"] is True
    assert saved["axes"]["Beta"]["zeroed"]
    assert saved["axes"]["Beta"]["angle"] == pytest.approx(3.0, abs=0.05)

    cfg2 = _cfg(tmp_path, park_on_disconnect=False,
                restore_position=True)
    dev2 = StingDrive(cfg2)
    msgs = []
    dev2.on_status = msgs.append
    dev2.connect()
    try:
        st = dev2.state()["Beta"]
        assert st["zeroed"]
        assert st["angle"] == pytest.approx(3.0, abs=0.05)
        assert any("Restored last position" in m for m in msgs)
        # clean shutdown → no scary warning
        assert not any("VERIFY" in m for m in msgs)
    finally:
        dev2.disconnect()


def test_unclean_shutdown_restores_with_warning(tmp_path):
    state = {
        "saved_at": "2026-07-22T10:30:00",
        "clean": False,                       # process died mid-session
        "axes": {
            "Alpha": {"angle": 12.5, "counts": 0, "zeroed": True,
                      "zero_offset_deg": 12.5},
            "Beta": {"angle": 0.0, "counts": 0, "zeroed": False,
                     "zero_offset_deg": 0.0},
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    cfg = _cfg(tmp_path, park_on_disconnect=False, restore_position=True)
    dev = StingDrive(cfg)
    msgs = []
    dev.on_status = msgs.append
    dev.connect()
    try:
        st = dev.state()["Alpha"]
        assert st["zeroed"]
        assert st["angle"] == pytest.approx(12.5, abs=1e-6)
        assert not dev.state()["Beta"]["zeroed"]   # was never zeroed
        assert any("did NOT shut down cleanly" in m for m in msgs)
        assert any("VERIFY" in m for m in msgs)
    finally:
        dev.disconnect()


def test_running_session_checkpoints_periodically(tmp_path):
    cfg = _cfg(tmp_path, park_on_disconnect=False,
               restore_position=False)
    dev = StingDrive(cfg)
    dev.connect()
    try:
        dev.set_current_angle("alpha", 5.0)

        def _saved_alpha():
            try:
                s = json.loads((tmp_path / "state.json").read_text())
                return s["axes"]["Alpha"]["angle"]
            except Exception:                    # noqa: BLE001
                return None

        # the periodic checkpoint (2 s) converges on the live angle
        assert _wait(lambda: _saved_alpha() == 5.0, 6.0)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["clean"] is False           # still running
    finally:
        dev.disconnect()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["clean"] is True                # orderly shutdown
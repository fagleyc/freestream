"""Serial-adapter config provenance + COM-port resolution (sim/mocks only).

Rig-found 2026-07: the LSWT sting and the Heise connected fine in their
STANDALONE apps but failed through Freestream. Root cause: the adapters
built factory driver configs (sting placeholder COM1 / heise blank port)
and never ran the packages' comscan the way the standalone Search button
does. These tests pin the fix:

* LIVE adapter construction loads the device's OWN startup defaults
  (``defaults_path()`` env-overridable), SIM stays hermetic factory.
* A blank COM port at LIVE connect runs the package comscan ONCE and
  adopts the hit, or fails with a clear actionable message.
* A configured-but-silent port gets ONE rescue scan before the original
  error propagates.
* ``force_sim`` stays session-owned through defaults and bundles.

NO real serial ports are ever opened — drivers are replaced by fakes and
comscan.search is monkeypatched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.heise import HeiseAdapter             # noqa: E402
from freestream.adapters.lswt_sting import LswtStingAdapter    # noqa: E402

import heise.comscan as heise_comscan                          # noqa: E402
import lswt_sting.comscan as sting_comscan                     # noqa: E402
from heise.config import HeiseConfig                           # noqa: E402
from lswt_sting.config import StingConfig                      # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────
class _FakeDev:
    """Stands in for HeiseGauge/StingDrive — NEVER opens a port."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.connect_calls = 0
        self.connected = False

    def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_times:
            raise RuntimeError("device not responding on the "
                               "configured port")
        self.connected = True

    def frame_count(self):
        return 0


def _heise_hit(port="COM5"):
    return heise_comscan.ProbeResult(
        port=heise_comscan.PortInfo(port, "USB Serial"), opened=True,
        baud=9600, response="14.7,72.1", is_heise=True)


def _heise_miss(port="COM3"):
    return heise_comscan.ProbeResult(
        port=heise_comscan.PortInfo(port), opened=True)


def _sting_hit(port="COM5"):
    return sting_comscan.ProbeResult(
        port=sting_comscan.PortInfo(port, "Prolific USB-to-Serial"),
        opened=True, response="*R", is_sting=True)


@pytest.fixture
def isolated_defaults(tmp_path, monkeypatch):
    """Point every device-defaults file into tmp (none exist yet)."""
    monkeypatch.setenv("LSWT_STING_DEFAULTS",
                       str(tmp_path / "sting_defaults.json"))
    monkeypatch.setenv("HEISE_DEFAULTS",
                       str(tmp_path / "heise_defaults.json"))
    return tmp_path


# ── config provenance: device defaults → adapter ─────────────────────────
def test_sting_live_adapter_loads_device_defaults(isolated_defaults):
    cfg = StingConfig()
    cfg.com_port = "COM7"                  # the rig-proven port (marker)
    cfg.poll_ms = 123
    cfg.force_sim = True                   # stale flag must NOT leak in
    cfg.save(isolated_defaults / "sting_defaults.json")

    a = LswtStingAdapter(sim=False)
    assert a.config.com_port == "COM7"
    assert a.config.poll_ms == 123
    # manager owns SIM/LIVE — a saved force_sim never flips the session
    assert a.config.force_sim is False


def test_sting_sim_adapter_stays_factory(isolated_defaults):
    StingConfig().save(isolated_defaults / "sting_defaults.json")
    marker = StingConfig.load(isolated_defaults / "sting_defaults.json")
    marker.com_port = "COM7"
    marker.save(isolated_defaults / "sting_defaults.json")

    a = LswtStingAdapter(sim=True)         # hermetic sim: factory config
    assert a.config.com_port == StingConfig().com_port
    assert a.config.force_sim is True


def test_heise_live_adapter_loads_device_defaults(isolated_defaults):
    cfg = HeiseConfig()
    cfg.com_port = "COM9"
    cfg.poll_s = 0.123
    cfg.force_sim = True
    cfg.save(isolated_defaults / "heise_defaults.json")

    a = HeiseAdapter(sim=False)
    assert a.config.com_port == "COM9"
    assert a.config.poll_s == 0.123
    assert a.config.force_sim is False
    # canonical channel names are re-asserted over whatever was saved
    assert {p.name for p in a.config.enabled_ports()} == {"Ptot", "Temp"}


def test_heise_defaults_units_not_stomped(isolated_defaults):
    """A defaults file's units survive; only pure-factory configs get
    the derived-chain unit declaration (psi / C)."""
    cfg = HeiseConfig()
    cfg.com_port = "COM9"
    cfg.left.unit = "kPa"                  # operator's saved choice (pressure=LEFT)
    cfg.save(isolated_defaults / "heise_defaults.json")

    a = HeiseAdapter(sim=False)
    pressure = [p for p in a.config.ports() if p.role == "pressure"][0]
    assert pressure.unit == "kPa"


def test_heise_factory_units_when_no_defaults(isolated_defaults):
    a = HeiseAdapter(sim=False)            # no defaults file present
    units = {p.role: p.unit for p in a.config.ports()}
    assert units["pressure"] == "psi"
    assert units["temperature"] == "F"     # RTD default deg F (Casey)


def test_freestream_bundle_overrides_device_defaults(isolated_defaults):
    """Precedence: device defaults → freestream bundle → session SIM."""
    cfg = HeiseConfig()
    cfg.com_port = "COM9"
    cfg.save(isolated_defaults / "heise_defaults.json")

    a = HeiseAdapter(sim=False)
    a.apply_config_dict(dict(a.config_dict(), com_port="COM4",
                             force_sim=True))
    assert a.config.com_port == "COM4"     # bundle wins over defaults
    assert a.config.force_sim is False     # session still owns SIM/LIVE


# ── blank COM port at LIVE connect → one scan ────────────────────────────
def test_heise_blank_port_connect_runs_scan_and_adopts_hit(
        isolated_defaults, monkeypatch):
    calls = []
    monkeypatch.setattr(heise_comscan, "search",
                        lambda *a, **k: calls.append(1) or
                        [_heise_miss("COM3"), _heise_hit("COM5")])
    a = HeiseAdapter(sim=False)
    assert a.config.com_port == ""         # factory blank
    fake = _FakeDev()
    a._dev = fake

    a.connect()
    assert a.config.com_port == "COM5"
    assert fake.connect_calls == 1
    assert len(calls) == 1                 # scan ran exactly ONCE


def test_heise_blank_port_scan_empty_raises_clear_error(
        isolated_defaults, monkeypatch):
    calls = []
    monkeypatch.setattr(heise_comscan, "search",
                        lambda *a, **k: calls.append(1) or [])
    a = HeiseAdapter(sim=False)
    fake = _FakeDev()
    a._dev = fake

    with pytest.raises(RuntimeError, match="COM port not configured"):
        a.connect()
    assert fake.connect_calls == 0         # never tried a blank open
    assert len(calls) == 1


def test_sim_connect_never_scans(isolated_defaults, monkeypatch):
    def _boom(*a, **k):                    # scan must not run in sim
        raise AssertionError("comscan ran in sim mode")
    monkeypatch.setattr(heise_comscan, "search", _boom)
    a = HeiseAdapter(sim=True)
    a.connect()                            # emulator — no port needed
    try:
        assert a.connected
    finally:
        a.disconnect()


# ── configured-but-silent port → one rescue scan ─────────────────────────
def test_sting_wrong_port_rescued_by_scan(isolated_defaults, monkeypatch):
    monkeypatch.setattr(sting_comscan, "search",
                        lambda *a, **k: [_sting_hit("COM5")])
    a = LswtStingAdapter(sim=False)
    assert a.config.com_port == "COM1"     # factory placeholder
    fake = _FakeDev(fail_times=1)          # COM1 does not answer
    a._dev = fake

    a.connect()
    assert a.config.com_port == "COM5"     # scan found the chain
    assert fake.connect_calls == 2         # failed once, retried once
    assert fake.connected


def test_sting_rescue_finds_nothing_original_error_propagates(
        isolated_defaults, monkeypatch):
    monkeypatch.setattr(sting_comscan, "search", lambda *a, **k: [])
    a = LswtStingAdapter(sim=False)
    fake = _FakeDev(fail_times=99)
    a._dev = fake

    with pytest.raises(RuntimeError, match="not responding"):
        a.connect()
    assert a.config.com_port == "COM1"     # untouched
    assert fake.connect_calls == 1         # no blind retry


def test_sting_rescue_same_port_no_retry(isolated_defaults, monkeypatch):
    """Scan answering on the ALREADY-configured port is not a rescue —
    the original failure propagates instead of a doomed retry loop."""
    monkeypatch.setattr(sting_comscan, "search",
                        lambda *a, **k: [_sting_hit("COM1")])
    a = LswtStingAdapter(sim=False)
    fake = _FakeDev(fail_times=99)
    a._dev = fake

    with pytest.raises(RuntimeError, match="not responding"):
        a.connect()
    assert fake.connect_calls == 1


# ── traverse / lswt: same latent flaw, same fix ──────────────────────────
def test_traverse_live_adapter_loads_device_defaults(tmp_path,
                                                     monkeypatch):
    from freestream.adapters.traverse import TraverseAdapter
    from traverse_swt.config import TraverseConfig
    p = tmp_path / "trav_defaults.json"
    monkeypatch.setenv("TRAVERSE_DEFAULTS", str(p))
    cfg = TraverseConfig()
    cfg.loop_ms = 123
    cfg.force_sim = True
    cfg.save(p)

    a = TraverseAdapter(sim=False)
    assert a.config.loop_ms == 123
    assert a.config.force_sim is False
    # sim stays hermetic factory
    assert TraverseAdapter(sim=True).config.loop_ms == \
        TraverseConfig().loop_ms


def test_lswt_live_adapter_loads_tunnel_defaults(tmp_path, monkeypatch):
    from freestream.adapters.lswt import LswtTunnelAdapter
    from lswt.config import LswtConfig, defaults_path
    monkeypatch.setenv("LSWT_DEFAULTS", str(tmp_path))
    cfg = LswtConfig.for_tunnel("north")
    cfg.ip = "192.168.9.9"                 # the operator's saved drive IP
    defaults_path("north").parent.mkdir(parents=True, exist_ok=True)
    cfg.save(defaults_path("north"))

    a = LswtTunnelAdapter(sim=False)
    assert a.config.ip == "192.168.9.9"
    assert a.config.force_sim is False
    # sim stays hermetic factory (placeholder IP)
    assert LswtTunnelAdapter(sim=True).config.ip == \
        LswtConfig.for_tunnel("north").ip

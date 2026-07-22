"""Pure protocol tests — no sockets, no hardware.

Run directly (``python tests/test_protocol.py``) or via pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance import protocol as P


def test_loads_roundtrip_with_sync():
    vals = (1.5, -2.25, 3.0, 0.0, 100.5, -0.125)
    pkt = P.decode_loads(P.encode_loads(vals, sync=1, with_sync=True))
    assert pkt is not None
    assert pkt.values == vals
    assert pkt.sync == 1
    assert pkt.had_sync is True


def test_loads_roundtrip_no_sync():
    vals = (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    raw = P.encode_loads(vals, with_sync=False)
    assert len(raw) == P.LOADS_LEN_NO_SYNC == 29
    pkt = P.decode_loads(raw)
    assert pkt is not None
    assert pkt.values == vals
    assert pkt.had_sync is False


def test_loads_lengths():
    assert P.LOADS_LEN_WITH_SYNC == 33
    assert len(P.encode_loads([0.0] * 6, with_sync=True)) == 33


def test_decode_rejects_garbage():
    assert P.decode_loads(b"") is None
    assert P.decode_loads(b"NOISE" + b"\x00" * 24) is None
    assert P.decode_loads(b"LOADS" + b"\x00" * 10) is None   # too short


def test_loads_to_named_wire_order():
    named = P.loads_to_named((1, 2, 3, 4, 5, 6))
    assert named == {"Lift": 1.0, "Pitch": 2.0, "Drag": 3.0,
                     "Side": 4.0, "Yaw": 5.0, "Roll": 6.0}


def test_build_command():
    assert P.build_command(1234, "GOTO_YAW_POS", 12.5) == b"{M1234:GOTO_YAW_POS 12.5}"
    assert P.build_command(7, "ZERO") == b"{M7:ZERO}"


def test_parse_message_reply():
    m = P.parse_message("R1234:YAW_COMPLETE 12.50")
    assert m is not None
    assert (m.key, m.serial, m.command, m.params) == \
        ("R", 1234, "YAW_COMPLETE", ["12.50"])
    assert m.float_params() == [12.5]


def test_parse_message_comma_and_space_tokens():
    m = P.parse_message("R9:POSITIONS 10.0,-5.0")
    assert m is not None
    assert m.float_params() == [10.0, -5.0]


def test_parse_message_malformed():
    assert P.parse_message("") is None
    assert P.parse_message("X1:FOO") is None       # bad key char
    assert P.parse_message("Mabc:FOO") is None     # non-integer serial
    assert P.parse_message("M12:") is None         # empty body


def test_extract_messages_split_and_junk():
    msgs, rem = P.extract_messages("junk{M1:ZERO}{R1:TARES 0 0 0 0 0 0}{M2:GET_")
    assert msgs == ["M1:ZERO", "R1:TARES 0 0 0 0 0 0"]
    assert rem == "{M2:GET_"
    # feed the rest
    msgs2, rem2 = P.extract_messages(rem + "POSITIONS}")
    assert msgs2 == ["M2:GET_POSITIONS"]
    assert rem2 == ""


def test_extract_messages_overflow_guard():
    _, rem = P.extract_messages("{" + "x" * 5000)
    assert rem == ""    # stuck-link buffer discarded


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} protocol tests passed.")


if __name__ == "__main__":
    _run_all()

"""ATE external balance — wire protocol (pure, socket-free).

Implements the message formats documented in the ATE 6-Component Underfloor
Balance *Operations Manual* (AID-010-10015-1, section 6 "Client Communication
Facilities") and cross-checked against the OGI client reference source
(``OGI/Source/USAFA_Code.pas``).

Three logical channels exist between the balance's OGI control PC and the
external "TMS" client (this software):

  * **TMSC**  (control)  - TCP, ASCII, brace-framed commands/replies
  * **TMSD**  (data)     - UDP, binary continuous load stream ("LOADS")
  * **OGIT**  (trigger)  - UDP, ASCII "TMS_CONNECT" to (re)establish the link

Nothing in this module touches a socket; everything is ``bytes`` <-> Python so
it can be unit-tested without hardware.  The socket/threading layer lives in
:mod:`ate_balance.device`; a pure-Python OGI stand-in lives in
:mod:`ate_balance.emulator`.

Message reference
-----------------
Control (TMSC), framed in braces, asynchronous, serial-tagged::

    client -> OGI : {M1234:GOTO_YAW_POS 12.5}
    OGI -> client : {R1234:YAW_MOVING}
    OGI -> client : {R1234:YAW_COMPLETE 12.50}

Data (TMSD), one UDP datagram per scan, network (big-endian) byte order::

    b"LOADS" + 6 x float32 (Lift,Pitch,Drag,Side,Yaw,Roll) [+ int32 sync]

The trailing int32 "sync" flag is specified by the manual (33-byte packet) but
omitted by the shipped USAFA build (29-byte packet); :func:`decode_loads`
accepts either and reports which was seen.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────
#  Defaults (Operations Manual section 6: "By default ... TMSC=3040;
#  TMSD=3041; OGIT=3042.")
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_TMSC_PORT = 3040   # TCP  control
DEFAULT_TMSD_PORT = 3041   # UDP  data
DEFAULT_OGIT_PORT = 3042   # UDP  trigger

# ── TCP framing characters (brace-delimited messages) ──
STX = "{"
ETX = "}"

# ── Message identifier key characters ──
KEY_MESSAGE = "M"   # original message, client -> OGI
KEY_REPLY = "R"     # reply, OGI -> client

# ── Data-stream opcode (fixed 5-char ASCII on every TMSD datagram) ──
LOADS_OPCODE = b"LOADS"

# ── Axis order *on the wire* (Manual: "always transmitted in the order:
#     Lift, Pitch, Drag, Side-force, Yaw, and Roll"). ──
WIRE_AXES: Tuple[str, ...] = ("Lift", "Pitch", "Drag", "Side", "Yaw", "Roll")

# Map a wire-ordered 6-tuple to the Streamlined grouping (forces then moments).
# Streamlined names: Lift, Drag, Side (forces); Roll, Pitch, Yaw (moments).
_WIRE_INDEX = {name: i for i, name in enumerate(WIRE_AXES)}


def loads_to_named(values: Sequence[float]) -> dict:
    """Map a wire-ordered 6-tuple -> dict keyed by axis name.

    >>> loads_to_named((1, 2, 3, 4, 5, 6))["Pitch"]
    2.0
    """
    return {name: float(values[_WIRE_INDEX[name]]) for name in WIRE_AXES}


# ─────────────────────────────────────────────────────────────────────────
#  TMSD — binary load datagram
# ─────────────────────────────────────────────────────────────────────────

_FMT_6F = struct.Struct(">6f")    # 24 bytes
_FMT_I = struct.Struct(">i")      # 4 bytes

LOADS_LEN_WITH_SYNC = len(LOADS_OPCODE) + _FMT_6F.size + _FMT_I.size   # 33
LOADS_LEN_NO_SYNC = len(LOADS_OPCODE) + _FMT_6F.size                   # 29


@dataclass
class LoadsPacket:
    """A decoded TMSD load datagram.

    Attributes
    ----------
    values : tuple of float
        The six loads in wire order (Lift, Pitch, Drag, Side, Yaw, Roll),
        in N and N.m.
    sync : int
        Synchronisation input state (0/1); 0 if the packet carried no sync int.
    had_sync : bool
        True if the trailing int32 sync word was present on the wire.
    """
    values: Tuple[float, float, float, float, float, float]
    sync: int = 0
    had_sync: bool = False

    @property
    def named(self) -> dict:
        return loads_to_named(self.values)


def decode_loads(datagram: bytes) -> Optional[LoadsPacket]:
    """Decode one TMSD datagram, or return ``None`` if it is not a LOADS packet.

    Accepts both the 33-byte (manual) and 29-byte (USAFA build) layouts.
    """
    if len(datagram) < LOADS_LEN_NO_SYNC:
        return None
    if datagram[:5] != LOADS_OPCODE:
        return None
    body = datagram[5:]
    values = _FMT_6F.unpack(body[:_FMT_6F.size])
    if len(body) >= _FMT_6F.size + _FMT_I.size:
        sync = _FMT_I.unpack(body[_FMT_6F.size:_FMT_6F.size + _FMT_I.size])[0]
        return LoadsPacket(values, int(sync), had_sync=True)
    return LoadsPacket(values, 0, had_sync=False)


def encode_loads(values: Sequence[float], sync: int = 0,
                 with_sync: bool = True) -> bytes:
    """Encode six wire-ordered loads into a TMSD datagram (for the emulator)."""
    if len(values) != 6:
        raise ValueError("LOADS requires exactly 6 values (wire order L,P,D,S,Y,R)")
    out = LOADS_OPCODE + _FMT_6F.pack(*(float(v) for v in values))
    if with_sync:
        out += _FMT_I.pack(int(sync))
    return out


# ─────────────────────────────────────────────────────────────────────────
#  TMSC — brace-framed ASCII command / reply
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedMessage:
    """A parsed TMSC message.

    ``key`` is 'M' (message) or 'R' (reply); ``serial`` is the integer tag
    echoed between request and reply; ``command`` is the upper-cased keyword;
    ``params`` are the remaining whitespace/comma-separated tokens.
    """
    key: str
    serial: int
    command: str
    params: List[str] = field(default_factory=list)
    raw: str = ""

    def float_params(self) -> List[float]:
        out: List[float] = []
        for p in self.params:
            try:
                out.append(float(p))
            except ValueError:
                pass
        return out


def frame(content: str) -> bytes:
    """Wrap a message body in the ``{ }`` framing characters and encode ASCII."""
    return (STX + content + ETX).encode("ascii", errors="replace")


def build_command(serial: int, command: str, *params) -> bytes:
    """Build a framed client->OGI command.

    >>> build_command(1234, "GOTO_YAW_POS", 12.5)
    b'{M1234:GOTO_YAW_POS 12.5}'
    """
    body = f"{KEY_MESSAGE}{int(serial)}:{command}"
    for p in params:
        body += " " + _fmt_param(p)
    return frame(body)


def build_reply(serial: int, content: str) -> bytes:
    """Build a framed OGI->client reply (used by the emulator)."""
    return frame(f"{KEY_REPLY}{int(serial)}:{content}")


def _fmt_param(p) -> str:
    if isinstance(p, float):
        # Trim to a compact-but-faithful representation.
        return f"{p:g}"
    return str(p)


def extract_messages(buffer: str) -> Tuple[List[str], str]:
    """Pull complete ``{...}`` messages out of a TCP receive buffer.

    Port of ``AssembleMessageWithBraces_TMSC`` in USAFA_Code.pas: tolerates
    junk before a ``{``, splits on the first matching ``}``, and returns any
    unterminated remainder so it can be prepended to the next chunk.

    Returns ``(messages, remainder)`` where each message is the text *between*
    the braces (framing stripped).
    """
    messages: List[str] = []
    while True:
        start = buffer.find(STX)
        if start < 0:
            return messages, ""          # nothing started; discard junk
        if start > 0:
            buffer = buffer[start:]      # drop leading junk
        end = buffer.find(ETX)
        if end < 0:
            # Guard against unbounded growth on a stuck link.
            if len(buffer) > 4096:
                buffer = ""
            return messages, buffer      # incomplete; keep remainder
        messages.append(buffer[1:end])   # strip { and }
        buffer = buffer[end + 1:]


def parse_message(content: str) -> Optional[ParsedMessage]:
    """Parse one un-framed TMSC message body, or ``None`` if malformed.

    >>> m = parse_message("R1234:YAW_COMPLETE 12.50")
    >>> m.key, m.serial, m.command, m.params
    ('R', 1234, 'YAW_COMPLETE', ['12.50'])
    """
    colon = content.find(":")
    if colon < 2:                        # need key char + >=1 serial digit
        return None
    key = content[0].upper()
    if key not in (KEY_MESSAGE, KEY_REPLY):
        return None
    try:
        serial = int(content[1:colon])
    except ValueError:
        return None
    rest = content[colon + 1:].strip()
    if not rest:
        return None
    # Delphi's CommaText treats both spaces and commas as delimiters.
    tokens = [t for t in rest.replace(",", " ").split() if t]
    if not tokens:
        return None
    return ParsedMessage(key=key, serial=serial,
                         command=tokens[0].upper(), params=tokens[1:],
                         raw=content)


# ─────────────────────────────────────────────────────────────────────────
#  Command vocabulary (Operations Manual section 6 + USAFA_Code.pas handlers)
# ─────────────────────────────────────────────────────────────────────────

# Commands the client may send (TMS -> OGI).
CMD_ZERO = "ZERO"
CMD_TAKE_SAMPLE = "TAKE_SAMPLE"          # + duration seconds (1..300)
CMD_LOCK = "LOCK_BAL"
CMD_UNLOCK = "UNLOCK_BAL"
CMD_GET_LOCK_STATUS = "GET_LOCK_STATUS"
CMD_GET_POSITIONS = "GET_POSITIONS"
CMD_GOTO_YAW = "GOTO_YAW_POS"            # + degrees (-90..90)
CMD_GOTO_INC = "GOTO_INC_POS"            # + degrees (-10..45)
CMD_GET_FILTERS = "GET_FILTERS"
CMD_STOP_ALL = "STOP_ALL_MOTION"
CMD_KILL_YI = "KILL_YI_DRIVES"

# Reply keywords the client may receive (OGI -> TMS).
RSP_TARES = "TARES"
RSP_SAMPLES = "SAMPLES"
RSP_BAL_LOCKED = "BAL_LOCKED"
RSP_BAL_UNLOCKED = "BAL_UNLOCKED"
RSP_LOCK_STATUS = "LOCK_STATUS"
RSP_POSITIONS = "POSITIONS"
RSP_YAW_MOVING = "YAW_MOVING"
RSP_YAW_COMPLETE = "YAW_COMPLETE"
RSP_INC_MOVING = "INC_MOVING"
RSP_INC_COMPLETE = "INC_COMPLETE"
RSP_FILTERS = "FILTERS"
RSP_ERROR = "ERROR"

# Trigger payload (TMS -> OGIT) that prompts the OGI to (re)connect.
TRIGGER_CONNECT = "TMS_CONNECT"

# Documented motion limits (USAFA_Code.pas HandleTMSCMessage).
YAW_LIMITS_DEG = (-90.0, 90.0)
INC_LIMITS_DEG = (-10.0, 45.0)
SAMPLE_SECONDS_LIMITS = (1, 300)

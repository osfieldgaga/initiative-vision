"""CAN frame sources: the SocketCAN interface, and a fake one for testing.

Mirrors ``frames/source.py``'s shape deliberately (a ``CanFrame`` dataclass, a
``CanSource`` protocol, a fake implementation testable anywhere) so the CAN
pipeline can reuse the same queue/writer/drop-accounting design as the frame
pipeline almost unchanged -- see ``can/pipeline.py``.

**This module never transmits on the bus.** No function here calls
``socket.send()``/``sendto()`` under any code path, on principle, matching
the brief: this sprint is read-only logging, not actuation. The socket is
also recommended to be brought up in listen-only mode at the *interface*
level (``sudo ip link set can0 type can bitrate <rate> listen-only on``,
before Python ever touches it) -- that's a hardware/kernel guarantee,
independent of and in addition to this code's own promise, and it's what
stops the controller from even ACKing a frame, not just from us choosing not
to send one.

Timestamp clock (verified 2026-09-23 against a real kernel, not assumed --
see ``docs/timestamp-convention.md``): SocketCAN's kernel receive timestamp
(via ``SO_TIMESTAMPNS``) is on ``CLOCK_REALTIME``, *not* ``CLOCK_MONOTONIC``
-- a different clock than the camera's ``SensorTimestamp``, which is why
every ``CanFrame`` below carries two timestamps: ``timestamp_ns`` (our own
``CLOCK_MONOTONIC`` read, taken immediately after the frame is received --
the primary field, consistent with every other stream's convention) and
``kernel_realtime_ns`` (the raw kernel value, kept so the actual gap between
them can be measured on real hardware under real load, rather than assumed
to be negligible).

Synchronization with the frame stream does not need this gap measured at
all, though: ``CLOCK_MONOTONIC`` is one clock shared system-wide by every
process on the machine, so a frame session and a CAN session recorded at
the same time on the same Pi are already on a common ``timestamp_ns`` axis
by construction. See ``docs/timestamp-convention.md`` and
``can/pipeline.py``'s module docstring.
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

# struct can_frame (linux/can.h): canid_t can_id (u32), u8 can_dlc, 3 pad
# bytes, u8 data[8]. Verified empirically 2026-09-23 (send/receive round trip
# on a real vcan0 gave back exactly the bytes sent, size 16) rather than
# taken on faith from documentation alone.
_CAN_FRAME_FMT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(_CAN_FRAME_FMT)
assert CAN_FRAME_SIZE == 16

# canid_t flag bits (linux/can.h) -- the top 3 bits of can_id are flags, not
# part of the arbitration ID.
CAN_EFF_FLAG = 0x80000000  # frame uses a 29-bit extended ID, not 11-bit standard
CAN_RTR_FLAG = 0x40000000  # remote transmission request: a request, not data
CAN_ERR_FLAG = 0x20000000  # this is a controller-reported error, not a real frame
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF

# SO_TIMESTAMPNS (asm-generic/socket.h). Not exposed by name on Python's
# socket module on this kernel/build (checked: socket.SO_TIMESTAMPNS is
# absent), but this numeric value is stable across Linux architectures,
# including both this desktop (x86_64) and the Pi (aarch64) -- confirmed by
# testing it working on the Pi directly, not assumed portable.
SO_TIMESTAMPNS = 35
_ANCILLARY_BUFSIZE = 128  # generous for one timespec cmsg; real payload is 16 bytes


@dataclass(frozen=True)
class CanFrame:
    timestamp_ns: int          # our CLOCK_MONOTONIC read, taken right after recvmsg() returns
    kernel_realtime_ns: int | None  # the kernel's own CLOCK_REALTIME receive timestamp, if available
    can_id: int                 # arbitration ID only (flag bits already stripped)
    is_extended: bool           # 29-bit ID (True) vs 11-bit standard (False)
    is_remote_frame: bool       # RTR: a request, not data -- data is not meaningful if True
    dlc: int                    # data length code, 0-8 for classic CAN
    data: bytes                 # exactly dlc bytes


class CanSource(Protocol):
    def describe(self) -> dict:
        """Interface/bitrate/filter config for the manifest."""

    def start(self) -> None: ...

    def stop(self) -> None:
        """Ask ``frames()`` to end. Must be safe to call from another thread."""

    def frames(self) -> Iterator[CanFrame]: ...

    def close(self) -> None:
        """Release the socket. Called once, after ``frames()`` has finished."""


def _decode_can_frame(raw: bytes, timestamp_ns: int, kernel_realtime_ns: int | None) -> CanFrame:
    can_id_raw, dlc, data = struct.unpack(_CAN_FRAME_FMT, raw)
    is_extended = bool(can_id_raw & CAN_EFF_FLAG)
    is_remote = bool(can_id_raw & CAN_RTR_FLAG)
    mask = CAN_EFF_MASK if is_extended else CAN_SFF_MASK
    can_id = can_id_raw & mask
    return CanFrame(
        timestamp_ns=timestamp_ns,
        kernel_realtime_ns=kernel_realtime_ns,
        can_id=can_id,
        is_extended=is_extended,
        is_remote_frame=is_remote,
        dlc=dlc,
        data=data[:dlc],
    )


def _parse_kernel_timestamp(ancdata) -> int | None:
    """Pull a CLOCK_REALTIME nanosecond timestamp out of recvmsg()'s
    ancillary data, if SO_TIMESTAMPNS produced one. None if not present
    (e.g. the socket option wasn't set, or this driver doesn't support it --
    seen for real on vcan0 with the fancier SO_TIMESTAMPING, so this must
    degrade gracefully, not assume the kernel always provides it)."""
    for level, cmsg_type, cmsg_data in ancdata:
        if cmsg_type == SO_TIMESTAMPNS and len(cmsg_data) >= 16:
            sec, nsec = struct.unpack("=qq", cmsg_data[:16])
            return sec * 1_000_000_000 + nsec
    return None


class SocketCanSource:
    """Reads raw frames from a SocketCAN interface (``can0``, ``vcan0``, ...).

    Does not bring the interface up, set its bitrate, or put it in
    listen-only mode -- those are ``ip link`` operations done once, outside
    Python, before this runs (see the CAN sprint doc for the exact commands).
    This only opens a CAN_RAW socket and binds to an interface that must
    already exist and already be up.
    """

    def __init__(self, interface: str = "can0", can_filters: list | None = None):
        self.interface = interface
        # None = accept every ID (the default: "make it easy to add extra
        # data later" means capturing everything up front, since CAN
        # bandwidth is trivial next to the video stream -- decoding new
        # signals later is then just a DBC update, never a re-drive).
        # A list of (can_id, mask) pairs restricts what the kernel delivers,
        # for the rare case bus load genuinely needs cutting.
        self.can_filters = can_filters
        self._sock: socket.socket | None = None
        self._has_kernel_ts = False
        self._stop = threading.Event()

    def describe(self) -> dict:
        return {
            "kind": "socketcan",
            "interface": self.interface,
            "filters": self.can_filters,
        }

    def start(self) -> None:
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            if self.can_filters is not None:
                packed = b"".join(struct.pack("=II", can_id, mask) for can_id, mask in self.can_filters)
                sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, packed)
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
                self._has_kernel_ts = True
            except OSError:
                # Not fatal: falls back to timestamping in Python alone (see
                # _decode_can_frame's kernel_realtime_ns=None path).
                self._has_kernel_ts = False
            sock.bind((self.interface,))
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def frames(self) -> Iterator[CanFrame]:
        sock = self._sock
        # A short timeout, not blocking forever, so `stop()` (setting the
        # Event another thread is watching) is actually noticed promptly
        # even during a quiet bus (ignition off, car asleep) -- a truly
        # silent bus is expected, not a fault; see pipeline.py's stall
        # handling, which treats CAN silence differently from frame silence
        # for exactly this reason.
        sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                raw, ancdata, msg_flags, addr = sock.recvmsg(CAN_FRAME_SIZE, _ANCILLARY_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            if len(raw) != CAN_FRAME_SIZE:
                continue  # a partial/malformed read; extremely unlikely, but never crash on it
            timestamp_ns = time.monotonic_ns()
            kernel_realtime_ns = _parse_kernel_timestamp(ancdata) if self._has_kernel_ts else None
            frame = _decode_can_frame(raw, timestamp_ns, kernel_realtime_ns)
            if frame.can_id == 0 and frame.dlc == 0 and not raw.strip(b"\x00"):
                continue  # an all-zero read some drivers emit spuriously; not a real bus message
            yield frame

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


class FakeCanSource:
    """Deterministic stand-in for the bus, for tests and desktop runs.

    Cycles through a fixed list of (can_id, data) messages at a controllable
    rate -- enough to exercise the pipeline (queue, writer, drop accounting)
    without any CAN hardware, the same role FakeCameraSource plays for frames.
    """

    DEFAULT_MESSAGES = [
        (0x0C9, b"\x00\x01\x02\x03"),   # arbitrary placeholder IDs/payloads;
        (0x1F1, b"\xAA\xBB"),            # real IDs come from the vehicle's DBC,
        (0x3E5, b"\x10\x20\x30\x40\x50\x60\x70\x80"),  # not guessed here
    ]

    def __init__(
        self,
        hz: float = 100.0,
        num_frames: int | None = None,
        messages: list | None = None,
        realtime: bool = True,
        start_ns: int | None = None,
        jitter_ns: int = 0,
        seed: int = 0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.hz = hz
        self.num_frames = num_frames
        self.messages = messages if messages is not None else self.DEFAULT_MESSAGES
        self.realtime = realtime
        self.start_ns = start_ns
        self.jitter_ns = jitter_ns
        self._rng = random.Random(seed)
        self._clock_ns = clock_ns
        self._stop = threading.Event()

    def describe(self) -> dict:
        return {"kind": "fake", "synthetic": True, "target_hz": self.hz}

    def start(self) -> None:
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        pass

    def frames(self) -> Iterator[CanFrame]:
        interval_ns = round(1e9 / self.hz)
        t = self.start_ns if self.start_ns is not None else self._clock_ns()
        i = 0
        while not self._stop.is_set():
            if self.num_frames is not None and i >= self.num_frames:
                return
            if self.realtime:
                wait_s = (t - self._clock_ns()) / 1e9
                if wait_s > 0 and self._stop.wait(wait_s):
                    return
            jitter = self._rng.randint(-self.jitter_ns, self.jitter_ns) if self.jitter_ns else 0
            can_id, data = self.messages[i % len(self.messages)]
            yield CanFrame(
                timestamp_ns=t + jitter,
                kernel_realtime_ns=None,
                can_id=can_id,
                is_extended=can_id > CAN_SFF_MASK,
                is_remote_frame=False,
                dlc=len(data),
                data=data,
            )
            i += 1
            t += interval_ns

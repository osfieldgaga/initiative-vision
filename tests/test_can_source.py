import socket
import struct
import tempfile
import threading
import time
import unittest

from ivision.can.source import (
    CAN_EFF_FLAG,
    CAN_FRAME_SIZE,
    CAN_RTR_FLAG,
    CanFrame,
    FakeCanSource,
    SO_TIMESTAMPNS,
    SocketCanSource,
    _decode_can_frame,
    _parse_kernel_timestamp,
)

_FMT = "=IB3x8s"


def _pack(can_id, data):
    return struct.pack(_FMT, can_id, len(data), data.ljust(8, b"\x00"))


def _vcan_available(iface="vcan0") -> bool:
    """True if a CAN_RAW socket can actually bind to `iface` right now.
    False (not an error) on any machine without it set up -- the desktop
    this suite mostly runs on doesn't have vcan0; the Pi does (set up
    2026-09-23 specifically to validate this module for real)."""
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
        s.close()
        return True
    except OSError:
        return False


class DecodeCanFrameTest(unittest.TestCase):
    def test_standard_frame(self):
        raw = _pack(0x123, b"\xde\xad\xbe\xef")
        f = _decode_can_frame(raw, timestamp_ns=1000, kernel_realtime_ns=2000)
        self.assertEqual(f, CanFrame(1000, 2000, 0x123, False, False, 4, b"\xde\xad\xbe\xef"))

    def test_extended_frame_id_and_flag(self):
        raw = _pack(0x1ABCDE | CAN_EFF_FLAG, b"\x01")
        f = _decode_can_frame(raw, 0, None)
        self.assertEqual(f.can_id, 0x1ABCDE)
        self.assertTrue(f.is_extended)

    def test_remote_frame_flag(self):
        raw = _pack(0x42 | CAN_RTR_FLAG, b"")
        f = _decode_can_frame(raw, 0, None)
        self.assertTrue(f.is_remote_frame)
        self.assertEqual(f.can_id, 0x42)

    def test_standard_id_mask_strips_only_flag_bits(self):
        # A standard (non-extended) frame's ID never has bits above 11 set in
        # real traffic, but the mask must still be applied correctly if it did.
        raw = _pack(0x7FF, b"\x00")
        f = _decode_can_frame(raw, 0, None)
        self.assertEqual(f.can_id, 0x7FF)  # CAN_SFF_MASK boundary

    def test_data_truncated_to_dlc_even_if_padding_bytes_are_nonzero(self):
        raw = struct.pack(_FMT, 0x1, 3, b"\x01\x02\x03\xff\xff\xff\xff\xff")
        f = _decode_can_frame(raw, 0, None)
        self.assertEqual(f.data, b"\x01\x02\x03")


class ParseKernelTimestampTest(unittest.TestCase):
    def test_extracts_so_timestampns_value(self):
        # sec=5, nsec=250 -> 5_000_000_250 ns
        cmsg_data = struct.pack("=qq", 5, 250)
        ancdata = [(socket.SOL_SOCKET, SO_TIMESTAMPNS, cmsg_data)]
        self.assertEqual(_parse_kernel_timestamp(ancdata), 5_000_000_250)

    def test_none_when_absent(self):
        self.assertIsNone(_parse_kernel_timestamp([]))
        self.assertIsNone(_parse_kernel_timestamp([(socket.SOL_SOCKET, 99, b"\x00" * 16)]))

    def test_ignores_short_cmsg_data(self):
        self.assertIsNone(_parse_kernel_timestamp([(socket.SOL_SOCKET, SO_TIMESTAMPNS, b"\x00\x01")]))


class FakeCanSourceTest(unittest.TestCase):
    def test_cycles_through_default_messages_in_order(self):
        src = FakeCanSource(hz=1000, num_frames=7, realtime=False, start_ns=0)
        src.start()
        frames = list(src.frames())
        self.assertEqual(len(frames), 7)
        expected_ids = [m[0] for m in FakeCanSource.DEFAULT_MESSAGES]
        got_ids = [f.can_id for f in frames]
        self.assertEqual(got_ids, (expected_ids * 3)[:7])

    def test_timestamps_advance_by_the_interval(self):
        src = FakeCanSource(hz=100, num_frames=3, realtime=False, start_ns=1_000_000_000)
        frames = list(src.frames())
        diffs = [b.timestamp_ns - a.timestamp_ns for a, b in zip(frames, frames[1:])]
        for d in diffs:
            self.assertEqual(d, 10_000_000)  # 1/100 s in ns

    def test_kernel_realtime_ns_is_always_none(self):
        src = FakeCanSource(hz=50, num_frames=2, realtime=False, start_ns=0)
        for f in src.frames():
            self.assertIsNone(f.kernel_realtime_ns)

    def test_stop_ends_an_endless_source_promptly(self):
        src = FakeCanSource(hz=200, realtime=True)  # no num_frames: endless until stopped
        src.start()
        it = src.frames()
        next(it)
        threading.Timer(0.1, src.stop).start()
        count = 0
        t0 = time.monotonic()
        for _ in it:
            count += 1
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertGreater(count, 5)

    def test_custom_messages_are_used_instead_of_defaults(self):
        src = FakeCanSource(hz=100, num_frames=2, realtime=False, start_ns=0,
                            messages=[(0x999, b"\xAB")])
        frames = list(src.frames())
        self.assertTrue(all(f.can_id == 0x999 and f.data == b"\xAB" for f in frames))


@unittest.skipUnless(_vcan_available(), "vcan0 not available on this machine")
class SocketCanSourceRealInterfaceTest(unittest.TestCase):
    """Runs for real against vcan0 where it exists (set up on the Pi
    2026-09-23 specifically for this). Skips cleanly everywhere else --
    the desktop this suite mostly runs on has no vcan0 and needs a
    password for sudo to create one, so this is not forced."""

    def send_raw(self, can_id, data, extended=False):
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind(("vcan0",))
        flag = CAN_EFF_FLAG if extended else 0
        s.send(_pack(can_id | flag, data))
        s.close()

    def test_receives_and_decodes_a_real_frame_via_the_kernel(self):
        src = SocketCanSource(interface="vcan0")
        src.start()
        self.addCleanup(src.close)
        it = src.frames()
        threading.Timer(0.05, self.send_raw, args=(0x123, b"\xde\xad\xbe\xef")).start()
        f = next(it)
        self.assertEqual((f.can_id, f.is_extended, f.dlc, f.data), (0x123, False, 4, b"\xde\xad\xbe\xef"))

    def test_kernel_timestamp_is_populated_and_close_to_wall_clock(self):
        src = SocketCanSource(interface="vcan0")
        src.start()
        self.addCleanup(src.close)
        it = src.frames()
        threading.Timer(0.05, self.send_raw, args=(0x1, b"\x00")).start()
        f = next(it)
        self.assertIsNotNone(f.kernel_realtime_ns)
        self.assertLess(abs(f.kernel_realtime_ns - time.time_ns()) / 1e6, 1000)  # within 1s, generously

    def test_filter_admits_only_the_configured_id(self):
        src = SocketCanSource(interface="vcan0", can_filters=[(0x200, 0x7FF)])
        src.start()
        self.addCleanup(src.close)
        it = src.frames()
        threading.Timer(0.05, self.send_raw, args=(0x300, b"\x01")).start()   # filtered out
        threading.Timer(0.15, self.send_raw, args=(0x200, b"\x02")).start()   # passes
        f = next(it)
        self.assertEqual(f.can_id, 0x200)

    def test_extended_frame_over_the_real_kernel(self):
        src = SocketCanSource(interface="vcan0")
        src.start()
        self.addCleanup(src.close)
        it = src.frames()
        threading.Timer(0.05, self.send_raw, args=(0x1ABCDE, b"\x01\x02"), kwargs={"extended": True}).start()
        f = next(it)
        self.assertEqual(f.can_id, 0x1ABCDE)
        self.assertTrue(f.is_extended)

    def test_stop_makes_frames_return_promptly_even_with_no_traffic(self):
        src = SocketCanSource(interface="vcan0")
        src.start()
        it = src.frames()
        threading.Timer(0.1, src.stop).start()
        t0 = time.monotonic()
        list(it)  # nothing sent; should return once stop() is noticed, not hang
        self.assertLess(time.monotonic() - t0, 2.0)
        src.close()

    def test_close_is_idempotent(self):
        src = SocketCanSource(interface="vcan0")
        src.start()
        src.close()
        src.close()  # must not raise


if __name__ == "__main__":
    unittest.main()

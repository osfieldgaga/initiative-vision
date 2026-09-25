import unittest

from ivision.frames.source import FakeCameraSource, IntervalSource


class FakeClock:
    """Advances a small fixed step on every read. A generator's internal
    discard loop can call the clock many times without ever returning
    control to the test (see IntervalSource.frames()), so the clock must
    advance on its own as it's polled -- a test-driven .advance() between
    next() calls can't reach code that runs inside a single next() call."""

    def __init__(self, start_ns: int = 0, step_ns: int = 50_000_000):  # 50ms/read
        self.now_ns = start_ns
        self.step_ns = step_ns

    def __call__(self) -> int:
        self.now_ns += self.step_ns
        return self.now_ns


class IntervalSourceTest(unittest.TestCase):
    def test_keeps_roughly_one_frame_per_interval(self):
        # Interval 1.0s, clock advances 50ms/read -> ~20 reads per kept frame.
        inner = FakeCameraSource(fps=1000, num_frames=200, realtime=False, start_ns=0)
        wrapped = IntervalSource(inner, interval_s=1.0, clock_ns=FakeClock())
        wrapped.start()
        kept = list(wrapped.frames())
        # 200 inner frames / ~20 reads-per-keep =~ 10 kept, not close to 200.
        self.assertLess(len(kept), 30)
        self.assertGreater(len(kept), 3)

    def test_seq_is_renumbered_densely(self):
        inner = FakeCameraSource(fps=1000, num_frames=500, realtime=False, start_ns=0)
        wrapped = IntervalSource(inner, interval_s=0.5, clock_ns=FakeClock())
        wrapped.start()
        kept = list(wrapped.frames())
        self.assertGreater(len(kept), 1)  # otherwise this test proves nothing
        self.assertEqual([f.seq for f in kept], list(range(len(kept))))

    def test_first_frame_is_kept_immediately_not_after_a_full_interval(self):
        inner = FakeCameraSource(fps=1000, num_frames=1, realtime=False, start_ns=0)
        wrapped = IntervalSource(inner, interval_s=5.0, clock_ns=FakeClock())
        wrapped.start()
        kept = list(wrapped.frames())
        self.assertEqual(len(kept), 1)  # didn't discard the only frame waiting for a full interval

    def test_encode_is_passed_through_when_present(self):
        class SourceWithEncode(FakeCameraSource):
            def encode(self, frame):
                return b"encoded"

        inner = SourceWithEncode(fps=10, realtime=False, start_ns=0)
        wrapped = IntervalSource(inner, interval_s=1.0)
        self.assertTrue(hasattr(wrapped, "encode"))
        self.assertEqual(wrapped.encode(None), b"encoded")

    def test_encode_absent_when_wrapped_source_has_none(self):
        inner = FakeCameraSource(fps=10, realtime=False, start_ns=0)
        wrapped = IntervalSource(inner, interval_s=1.0)
        self.assertFalse(hasattr(wrapped, "encode"))

    def test_describe_merges_wrapped_source_info(self):
        inner = FakeCameraSource(fps=10)
        wrapped = IntervalSource(inner, interval_s=2.5)
        d = wrapped.describe()
        self.assertEqual(d["mode"], "interval")
        self.assertEqual(d["interval_s"], 2.5)
        self.assertEqual(d["kind"], "fake")  # from the wrapped source's own describe()

    def test_start_stop_close_delegate_to_wrapped_source(self):
        events = []

        class TrackingSource(FakeCameraSource):
            def start(self):
                events.append("start")
                super().start()

            def stop(self):
                events.append("stop")
                super().stop()

            def close(self):
                events.append("close")

        inner = TrackingSource(fps=10)
        wrapped = IntervalSource(inner, interval_s=1.0)
        wrapped.start()
        wrapped.stop()
        wrapped.close()
        self.assertEqual(events, ["start", "stop", "close"])


if __name__ == "__main__":
    unittest.main()

import csv
import tempfile
import unittest
from pathlib import Path

try:
    import cantools
except ImportError:
    cantools = None

from ivision.can.decode import _add_derived_longitudinal_accel, decode_session

_TEST_DBC = """\
BO_ 100 TestSpeedMsg: 4 XXX
 SG_ SpeedLeft : 7|16@0+ (0.1,0) [0|6553.5] "km/h" XXX
 SG_ SpeedRight : 23|16@0+ (0.1,0) [0|6553.5] "km/h" XXX

BO_ 200 TestOtherMsg: 1 XXX
 SG_ Value : 7|8@0+ (1,0) [0|255] "" XXX

BO_ 300 TestCollidingNameMsg: 1 XXX
 SG_ Value : 7|8@0+ (1,0) [0|255] "" XXX
"""


def write_can_csv(path, rows):
    """rows: list of dicts with at least seq, timestamp_ns, can_id (hex str
    like '0x64'), is_extended ('0'/'1'), dlc, data (hex str, no separators)."""
    columns = ["seq", "timestamp_ns", "kernel_realtime_ns", "can_id",
               "is_extended", "is_remote_frame", "dlc", "data"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])


@unittest.skipUnless(cantools is not None, "cantools not installed")
class DecodeSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.dbc_path = self.base / "test.dbc"
        self.dbc_path.write_text(_TEST_DBC)
        self.db = cantools.database.load_string(_TEST_DBC, database_format="dbc")

    def encode_row(self, seq, timestamp_ns, message_name, **signal_values):
        message = self.db.get_message_by_name(message_name)
        data = message.encode(signal_values)
        return {
            "seq": seq, "timestamp_ns": timestamp_ns, "kernel_realtime_ns": "",
            "can_id": f"0x{message.frame_id:X}", "is_extended": "0",
            "is_remote_frame": "0", "dlc": len(data), "data": data.hex(),
        }

    def read_decoded(self, session):
        with open(session / "decoded.csv", newline="") as f:
            return list(csv.DictReader(f))

    def test_decodes_into_one_combined_csv_alongside_can_csv(self):
        session = self.base / "session"
        session.mkdir()
        rows = [self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=10.0, SpeedRight=20.0)]
        write_can_csv(session / "can.csv", rows)

        summary = decode_session(session, self.dbc_path)

        self.assertEqual(summary["total_rows"], 1)
        self.assertEqual(summary["decoded_messages"], {"TestSpeedMsg": 1})
        self.assertEqual(summary["undecodable_ids"], {})
        self.assertEqual(summary["out_path"], str(session / "decoded.csv"))
        self.assertTrue((session / "decoded.csv").exists())
        self.assertFalse((session / "decoded").exists())  # no per-message subfolder anymore

        out_rows = self.read_decoded(session)
        self.assertEqual(len(out_rows), 1)
        self.assertEqual(out_rows[0]["message"], "TestSpeedMsg")
        self.assertAlmostEqual(float(out_rows[0]["TestSpeedMsg.SpeedLeft"]), 10.0)
        self.assertAlmostEqual(float(out_rows[0]["TestSpeedMsg.SpeedRight"]), 20.0)

    def test_unknown_id_is_counted_not_silently_dropped(self):
        session = self.base / "session2"
        session.mkdir()
        rows = [
            self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0),
            {"seq": 1, "timestamp_ns": 1_000_000_001, "kernel_realtime_ns": "",
             "can_id": "0x999", "is_extended": "0", "is_remote_frame": "0",
             "dlc": 1, "data": "00"},
        ]
        write_can_csv(session / "can.csv", rows)

        summary = decode_session(session, self.dbc_path)

        self.assertEqual(summary["total_rows"], 2)
        self.assertEqual(summary["decoded_messages"], {"TestSpeedMsg": 1})
        self.assertEqual(summary["undecodable_ids"], {"0x999": 1})
        self.assertEqual(len(self.read_decoded(session)), 1)  # no blank row for the undecodable one

    def test_rows_sorted_by_timestamp_regardless_of_csv_order(self):
        session = self.base / "session3"
        session.mkdir()
        rows = [
            self.encode_row(1, 2_000_000_000, "TestSpeedMsg", SpeedLeft=2.0, SpeedRight=2.0),
            self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0),
        ]
        write_can_csv(session / "can.csv", rows)

        decode_session(session, self.dbc_path)
        out_rows = self.read_decoded(session)
        self.assertEqual([r["timestamp_ns"] for r in out_rows], ["1000000000", "2000000000"])

    def test_multiple_message_types_share_one_file_with_blank_columns(self):
        session = self.base / "session4"
        session.mkdir()
        rows = [
            self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0),
            self.encode_row(1, 1_000_000_100, "TestOtherMsg", Value=42),
        ]
        write_can_csv(session / "can.csv", rows)

        decode_session(session, self.dbc_path)
        out_rows = self.read_decoded(session)
        self.assertEqual(len(out_rows), 2)
        self.assertIn("TestSpeedMsg.SpeedLeft", out_rows[0])
        self.assertIn("TestOtherMsg.Value", out_rows[0])
        # The TestSpeedMsg row has no TestOtherMsg data, and vice versa.
        self.assertEqual(out_rows[0]["message"], "TestSpeedMsg")
        self.assertEqual(out_rows[0]["TestOtherMsg.Value"], "")
        self.assertEqual(out_rows[1]["message"], "TestOtherMsg")
        self.assertEqual(out_rows[1]["TestSpeedMsg.SpeedLeft"], "")

    def test_colliding_signal_names_across_messages_stay_distinct_columns(self):
        session = self.base / "session5"
        session.mkdir()
        rows = [
            self.encode_row(0, 1_000_000_000, "TestOtherMsg", Value=7),
            self.encode_row(1, 1_000_000_100, "TestCollidingNameMsg", Value=99),
        ]
        write_can_csv(session / "can.csv", rows)

        decode_session(session, self.dbc_path)
        out_rows = self.read_decoded(session)
        self.assertEqual(out_rows[0]["TestOtherMsg.Value"], "7")
        self.assertEqual(out_rows[0]["TestCollidingNameMsg.Value"], "")
        self.assertEqual(out_rows[1]["TestCollidingNameMsg.Value"], "99")
        self.assertEqual(out_rows[1]["TestOtherMsg.Value"], "")

    def test_out_path_can_be_overridden(self):
        session = self.base / "session6"
        session.mkdir()
        write_can_csv(session / "can.csv",
                      [self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0)])
        custom = self.base / "custom_output.csv"
        summary = decode_session(session, self.dbc_path, out_path=custom)
        self.assertEqual(summary["out_path"], str(custom))
        self.assertTrue(custom.exists())


class DerivedLongitudinalAccelTest(unittest.TestCase):
    def rows_for(self, message_name, samples):
        """samples: [(timestamp_ns, seq, left_speed_kmh, right_speed_kmh), ...]"""
        return [[ts, seq, message_name, {"FLWheelSpd": left, "FRWheelSpd": right}]
                for ts, seq, left, right in samples]

    def test_constant_speed_gives_zero_acceleration(self):
        # 36 km/h = 10 m/s exactly, unchanging.
        rows = self.rows_for("EBCMWheelSpdFront", [
            (0, 0, 36.0, 36.0),
            (1_000_000_000, 1, 36.0, 36.0),
            (2_000_000_000, 2, 36.0, 36.0),
        ])
        signals_by_message = {"EBCMWheelSpdFront": {"FLWheelSpd", "FRWheelSpd"}}
        _add_derived_longitudinal_accel(rows, signals_by_message, "EBCMWheelSpdFront",
                                        "FLWheelSpd", "FRWheelSpd")
        self.assertNotIn("longitudinal_accel_mps2", rows[0][3])  # no previous sample
        self.assertAlmostEqual(rows[1][3]["longitudinal_accel_mps2"], 0.0)
        self.assertAlmostEqual(rows[2][3]["longitudinal_accel_mps2"], 0.0)

    def test_known_acceleration_from_a_linear_speed_ramp(self):
        # 0 -> 36 -> 72 km/h (0 -> 10 -> 20 m/s) over 1s steps: +10 m/s2 each step.
        rows = self.rows_for("EBCMWheelSpdFront", [
            (0, 0, 0.0, 0.0),
            (1_000_000_000, 1, 36.0, 36.0),
            (2_000_000_000, 2, 72.0, 72.0),
        ])
        signals_by_message = {"EBCMWheelSpdFront": {"FLWheelSpd", "FRWheelSpd"}}
        _add_derived_longitudinal_accel(rows, signals_by_message, "EBCMWheelSpdFront",
                                        "FLWheelSpd", "FRWheelSpd")
        self.assertAlmostEqual(rows[0][3]["vehicle_speed_mps"], 0.0)
        self.assertAlmostEqual(rows[1][3]["vehicle_speed_mps"], 10.0)
        self.assertAlmostEqual(rows[1][3]["longitudinal_accel_mps2"], 10.0, places=2)
        self.assertAlmostEqual(rows[2][3]["longitudinal_accel_mps2"], 10.0, places=2)
        self.assertEqual(signals_by_message["EBCMWheelSpdFront"],
                         {"FLWheelSpd", "FRWheelSpd", "vehicle_speed_mps", "longitudinal_accel_mps2"})

    def test_only_touches_rows_for_the_named_message_not_others_interleaved(self):
        rows = (self.rows_for("EBCMWheelSpdFront", [(0, 0, 0.0, 0.0), (1_000_000_000, 2, 36.0, 36.0)])
                + [[500_000_000, 1, "SomeOtherMsg", {"Foo": 1}]])
        rows.sort(key=lambda r: r[0])
        signals_by_message = {"EBCMWheelSpdFront": {"FLWheelSpd", "FRWheelSpd"}, "SomeOtherMsg": {"Foo"}}
        _add_derived_longitudinal_accel(rows, signals_by_message, "EBCMWheelSpdFront",
                                        "FLWheelSpd", "FRWheelSpd")
        other_row = next(r for r in rows if r[2] == "SomeOtherMsg")
        self.assertNotIn("vehicle_speed_mps", other_row[3])

    def test_missing_message_is_a_silent_noop(self):
        rows: list = []
        signals_by_message: dict = {}
        _add_derived_longitudinal_accel(rows, signals_by_message, "EBCMWheelSpdFront",
                                        "FLWheelSpd", "FRWheelSpd")  # must not raise

    def test_missing_signal_is_a_silent_noop(self):
        rows = self.rows_for("EBCMWheelSpdFront", [(0, 0, 1.0, 1.0)])
        signals_by_message = {"EBCMWheelSpdFront": {"FLWheelSpd"}}  # missing FRWheelSpd
        _add_derived_longitudinal_accel(rows, signals_by_message, "EBCMWheelSpdFront",
                                        "FLWheelSpd", "FRWheelSpd")
        self.assertNotIn("vehicle_speed_mps", rows[0][3])


if __name__ == "__main__":
    unittest.main()

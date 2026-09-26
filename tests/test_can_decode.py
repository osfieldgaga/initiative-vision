import csv
import tempfile
import unittest
from pathlib import Path

try:
    import cantools
except ImportError:
    cantools = None

from ivision.can.decode import add_derived_longitudinal_accel, decode_session

_TEST_DBC = """\
BO_ 100 TestSpeedMsg: 4 XXX
 SG_ SpeedLeft : 7|16@0+ (0.1,0) [0|6553.5] "km/h" XXX
 SG_ SpeedRight : 23|16@0+ (0.1,0) [0|6553.5] "km/h" XXX

BO_ 200 TestOtherMsg: 1 XXX
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

    def read_decoded(self, session, message_name):
        with open(session / "decoded" / f"{message_name}.csv", newline="") as f:
            return list(csv.DictReader(f))

    def test_decodes_known_message_into_its_own_csv(self):
        session = self.base / "session"
        session.mkdir()
        rows = [self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=10.0, SpeedRight=20.0)]
        write_can_csv(session / "can.csv", rows)

        summary = decode_session(session, self.dbc_path)

        self.assertEqual(summary["total_rows"], 1)
        self.assertEqual(summary["decoded_messages"], {"TestSpeedMsg": 1})
        self.assertEqual(summary["undecodable_ids"], {})

        out_rows = self.read_decoded(session, "TestSpeedMsg")
        self.assertEqual(len(out_rows), 1)
        self.assertEqual(out_rows[0]["timestamp_ns"], "1000000000")
        self.assertAlmostEqual(float(out_rows[0]["SpeedLeft"]), 10.0)
        self.assertAlmostEqual(float(out_rows[0]["SpeedRight"]), 20.0)

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

    def test_rows_sorted_by_timestamp_regardless_of_csv_order(self):
        session = self.base / "session3"
        session.mkdir()
        rows = [
            self.encode_row(1, 2_000_000_000, "TestSpeedMsg", SpeedLeft=2.0, SpeedRight=2.0),
            self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0),
        ]
        write_can_csv(session / "can.csv", rows)

        decode_session(session, self.dbc_path)
        out_rows = self.read_decoded(session, "TestSpeedMsg")
        self.assertEqual([r["timestamp_ns"] for r in out_rows], ["1000000000", "2000000000"])

    def test_multiple_message_types_get_separate_files(self):
        session = self.base / "session4"
        session.mkdir()
        rows = [
            self.encode_row(0, 1_000_000_000, "TestSpeedMsg", SpeedLeft=1.0, SpeedRight=1.0),
            self.encode_row(1, 1_000_000_100, "TestOtherMsg", Value=42),
        ]
        write_can_csv(session / "can.csv", rows)

        summary = decode_session(session, self.dbc_path)
        self.assertEqual(set(summary["decoded_messages"]), {"TestSpeedMsg", "TestOtherMsg"})
        self.assertTrue((session / "decoded" / "TestOtherMsg.csv").exists())


class DerivedLongitudinalAccelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = Path(self._tmp.name)

    def write_decoded(self, name, rows):
        with open(self.out_dir / f"{name}.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ns", "seq", "FLWheelSpd", "FRWheelSpd"])
            for r in rows:
                writer.writerow(r)

    def read_decoded(self, name):
        with open(self.out_dir / f"{name}.csv", newline="") as f:
            return list(csv.DictReader(f))

    def test_constant_speed_gives_zero_acceleration(self):
        # 36 km/h = 10 m/s exactly, unchanging.
        self.write_decoded("EBCMWheelSpdFront", [
            (0, 0, 36.0, 36.0),
            (1_000_000_000, 1, 36.0, 36.0),
            (2_000_000_000, 2, 36.0, 36.0),
        ])
        add_derived_longitudinal_accel(self.out_dir, "EBCMWheelSpdFront", "FLWheelSpd", "FRWheelSpd")
        rows = self.read_decoded("EBCMWheelSpdFront")
        self.assertEqual(rows[0]["longitudinal_accel_mps2"], "")  # no previous sample
        self.assertAlmostEqual(float(rows[1]["longitudinal_accel_mps2"]), 0.0)
        self.assertAlmostEqual(float(rows[2]["longitudinal_accel_mps2"]), 0.0)

    def test_known_acceleration_from_a_linear_speed_ramp(self):
        # 0 -> 36 -> 72 km/h (0 -> 10 -> 20 m/s) over 1s steps: +10 m/s2 each step.
        self.write_decoded("EBCMWheelSpdFront", [
            (0, 0, 0.0, 0.0),
            (1_000_000_000, 1, 36.0, 36.0),
            (2_000_000_000, 2, 72.0, 72.0),
        ])
        add_derived_longitudinal_accel(self.out_dir, "EBCMWheelSpdFront", "FLWheelSpd", "FRWheelSpd")
        rows = self.read_decoded("EBCMWheelSpdFront")
        self.assertAlmostEqual(float(rows[0]["vehicle_speed_mps"]), 0.0)
        self.assertAlmostEqual(float(rows[1]["vehicle_speed_mps"]), 10.0)
        self.assertAlmostEqual(float(rows[1]["longitudinal_accel_mps2"]), 10.0, places=2)
        self.assertAlmostEqual(float(rows[2]["longitudinal_accel_mps2"]), 10.0, places=2)

    def test_averages_left_and_right_when_they_differ(self):
        self.write_decoded("EBCMWheelSpdRear", [
            (0, 0, 0.0, 0.0),
            (1_000_000_000, 1, 30.0, 42.0),  # avg 36 km/h = 10 m/s
        ])
        add_derived_longitudinal_accel(self.out_dir, "EBCMWheelSpdRear", "FLWheelSpd", "FRWheelSpd")
        rows = self.read_decoded("EBCMWheelSpdRear")
        self.assertAlmostEqual(float(rows[1]["vehicle_speed_mps"]), 10.0)

    def test_missing_message_file_is_a_silent_noop(self):
        # Never wrote EBCMWheelSpdFront.csv at all -- must not raise.
        add_derived_longitudinal_accel(self.out_dir, "EBCMWheelSpdFront", "FLWheelSpd", "FRWheelSpd")

    def test_missing_signal_columns_is_a_silent_noop(self):
        with open(self.out_dir / "SomeMsg.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ns", "seq", "SomethingElse"])
            writer.writerow([0, 0, 1])
        add_derived_longitudinal_accel(self.out_dir, "SomeMsg", "FLWheelSpd", "FRWheelSpd")
        rows = self.read_decoded("SomeMsg")
        self.assertNotIn("vehicle_speed_mps", rows[0])


if __name__ == "__main__":
    unittest.main()

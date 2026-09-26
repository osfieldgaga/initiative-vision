"""Offline DBC decoder for a recorded CAN session -- verification only, never
part of the live logger (see ``source.py``'s docstring: raw capture stays
undecoded on the device, decoding is a separate, rerunnable step).

    python -m ivision.can.decode sessions/<id> --dbc dbc/gm_global_a_powertrain.dbc

Reads ``<session_dir>/can.csv``, decodes every row whose arbitration ID has a
matching message in the DBC, and writes one CSV per decoded message under
``<session_dir>/decoded/<MessageName>.csv`` (``timestamp_ns, seq``, then one
column per signal). Rows whose ID has no DBC entry are counted, not silently
dropped -- printed in the summary, so "how much of this bus is still
unidentified" stays visible rather than quietly discarded.

Needs ``cantools`` (``pip install ivision[decode]`` or plain ``pip install
cantools``) -- not a dependency of anything else in this project.

## Longitudinal acceleration, derived

The DBC has no direct longitudinal-accelerometer signal (GM's
``LateralAcceleration``, on ``EBCMVehicleDynamic``, is lateral only). Instead,
for each of ``EBCMWheelSpdFront`` and ``EBCMWheelSpdRear`` independently, this
averages that message's own left/right wheel speed pair into a
``vehicle_speed_mps`` column, then differentiates consecutive samples (using
each row's own logged ``timestamp_ns``, not an assumed fixed rate) into
``longitudinal_accel_mps2``. Kept as two independent estimates -- front and
rear are decoded from separate CAN messages arriving at separate times, not
synchronized to the same instant -- rather than merged into one signal, so
they can be cross-checked against each other: they should roughly agree if
the DBC and this derivation are both right. This is a plain finite
difference, nothing smoothed -- real speed samples are somewhat noisy
discrete measurements, so treat the derivative as a first, "does this look
physically sane" pass, not a clean signal. Filtering it is a real
improvement to make later, once it's clear the raw version isn't enough --
not something to build before that's actually known.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path


def load_database(dbc_path):
    import cantools

    return cantools.database.load_file(dbc_path)


def decode_session(session_dir, dbc_path, out_dir=None) -> dict:
    """Decode <session_dir>/can.csv against dbc_path. Returns a summary dict
    (also what the CLI prints); writes one CSV per decoded message name."""
    db = load_database(dbc_path)
    session_dir = Path(session_dir)
    out_dir = Path(out_dir) if out_dir is not None else session_dir / "decoded"
    out_dir.mkdir(parents=True, exist_ok=True)

    decoded_rows: dict = {}  # message name -> [(timestamp_ns, seq, {signal: value}), ...]
    undecodable: Counter = Counter()  # (can_id, is_extended) -> row count
    total_rows = 0

    with open(session_dir / "can.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total_rows += 1
            can_id = int(row["can_id"], 16)
            is_extended = row["is_extended"] == "1"
            try:
                message = db.get_message_by_frame_id(can_id)
            except KeyError:
                undecodable[(can_id, is_extended)] += 1
                continue
            data = bytes.fromhex(row["data"])
            try:
                signals = message.decode(data, allow_truncated=True)
            except Exception:
                undecodable[(can_id, is_extended)] += 1
                continue
            decoded_rows.setdefault(message.name, []).append(
                (int(row["timestamp_ns"]), int(row["seq"]), signals)
            )

    written = {}
    for name, rows in decoded_rows.items():
        rows.sort(key=lambda r: r[0])
        signal_names = sorted({key for _, _, sigs in rows for key in sigs})
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ns", "seq", *signal_names])
            for timestamp_ns, seq, sigs in rows:
                writer.writerow([timestamp_ns, seq, *(sigs.get(s, "") for s in signal_names)])
        written[name] = len(rows)

    add_derived_longitudinal_accel(out_dir, "EBCMWheelSpdFront", "FLWheelSpd", "FRWheelSpd")
    add_derived_longitudinal_accel(out_dir, "EBCMWheelSpdRear", "RLWheelSpd", "RRWheelSpd")

    return {
        "total_rows": total_rows,
        "decoded_messages": written,
        "undecodable_ids": {f"0x{can_id:X}{'x' if ext else ''}": n
                            for (can_id, ext), n in undecodable.items()},
    }


def add_derived_longitudinal_accel(out_dir: Path, message_name: str,
                                    left_signal: str, right_signal: str) -> None:
    """Rewrites <out_dir>/<message_name>.csv (already decoded by decode_session)
    with two extra columns: vehicle_speed_mps (average of the two wheel
    speeds, km/h -> m/s) and longitudinal_accel_mps2 (its derivative against
    consecutive rows' own timestamps). A no-op if that message wasn't
    decoded, or doesn't have both wheel-speed signals -- callable safely
    regardless of which messages a given session actually contains."""
    path = out_dir / f"{message_name}.csv"
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if left_signal not in fieldnames or right_signal not in fieldnames:
        return

    speeds_mps = [(float(row[left_signal]) + float(row[right_signal])) / 2 / 3.6 for row in rows]
    accel = [None]
    for i in range(1, len(rows)):
        dt_s = (int(rows[i]["timestamp_ns"]) - int(rows[i - 1]["timestamp_ns"])) / 1e9
        accel.append((speeds_mps[i] - speeds_mps[i - 1]) / dt_s if dt_s > 0 else None)

    new_fieldnames = [*fieldnames, "vehicle_speed_mps", "longitudinal_accel_mps2"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(new_fieldnames)
        for row, speed, a in zip(rows, speeds_mps, accel):
            writer.writerow([row[k] for k in fieldnames] + [round(speed, 4), round(a, 4) if a is not None else ""])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", help="a session directory containing can.csv")
    parser.add_argument("--dbc", required=True, help="path to the .dbc signal definition file")
    args = parser.parse_args(argv)

    try:
        summary = decode_session(args.session_dir, args.dbc)
    except ImportError:
        print("cantools is not installed. Run: pip install cantools", file=sys.stderr)
        return 2

    print(f"total rows: {summary['total_rows']}")
    print("decoded messages:")
    for name, n in sorted(summary["decoded_messages"].items()):
        print(f"  {name}: {n} rows")
    if summary["undecodable_ids"]:
        undecoded_total = sum(summary["undecodable_ids"].values())
        print(f"undecodable (no DBC entry or decode failed): {undecoded_total} rows, "
              f"{len(summary['undecodable_ids'])} distinct IDs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

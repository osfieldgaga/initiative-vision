"""Offline DBC decoder for a recorded CAN session -- verification only, never
part of the live logger (see ``source.py``'s docstring: raw capture stays
undecoded on the device, decoding is a separate, rerunnable step).

    python -m ivision.can.decode sessions/<id> --dbc dbc/gm_global_a_powertrain.dbc

Reads ``<session_dir>/can.csv``, decodes every row whose arbitration ID has a
matching message in the DBC, and writes **one combined CSV**,
``<session_dir>/decoded.csv``, alongside ``frames.csv``/``can.csv``/
``system.csv`` -- one row per decodable CAN frame (same row count and order
as the relevant rows of ``can.csv``), columns ``timestamp_ns, seq, message``
plus every signal from every decoded message, unioned together.

**Each row is a full snapshot, not just what changed.** Every column holds
the most recently decoded value for that signal as of that row's timestamp
-- carried forward ("last observation carried forward") from whichever
earlier row last updated it -- not just the columns belonging to that row's
own ``message``. So row N shows the complete, best-known state of every
signal at that instant, the way you'd read a dashboard: e.g. a row where
``message`` is ``PSCMSteeringAngle`` still shows the last-known
``EBCMWheelSpdFront.FLWheelSpd``, even though this particular frame didn't
touch it. A column stays blank only until that signal has been decoded at
least once; the ``message`` column still says which signal(s) this exact
row actually just updated, if you need that. One honest consequence of
forward-filling asynchronous, multi-rate signals: a slow-updating signal
(e.g. once a second) shows the *same* carried-forward value across many
faster-updating rows in between -- that's not a bug, it's what "most
recently known" genuinely means until the next real update arrives.

Signal columns are the **bare signal name** (e.g. ``SteeringWheelAngle``,
``BrakePedalPos``) -- not prefixed by the message name -- *except* for the
handful that would otherwise collide: a few signal names repeat across more
than one message in this DBC (``RollingCounter`` appears on six, for
instance), and using the bare name for those specifically would silently
conflate different signals. Only those get namespaced as
``<MessageName>.<SignalName>``; every unambiguous signal (the large
majority -- 109 of 113 in this DBC) keeps its plain name. Check the decode
summary's printed column list, or the header row of ``decoded.csv`` itself,
if you're not sure whether a given signal was ambiguous enough to need
qualifying.

Rows whose ID has no DBC entry are counted, not silently dropped -- printed
in the summary, so "how much of this bus is still unidentified" stays
visible rather than quietly discarded. They are not written as blank rows
in ``decoded.csv`` (no signals to show for them, and at typical undecoded
proportions that would mean hundreds of thousands of all-blank rows for no
benefit).

**Skipping messages or signals you don't want:** ``--skip`` excludes
something entirely -- decodable, just deliberately not wanted in the
output (as opposed to "undecodable", which means the DBC has no
definition for it at all). Repeatable, or comma-separate several in one
``--skip``. Each entry is one of three things, checked against the loaded
DBC (not just string-matched blindly):

* An arbitration ID, written with an explicit ``0x`` prefix (e.g.
  ``0x1E5``) -- skips that whole message.
* A message name (e.g. ``VIN_Part1``) -- same, skips the whole message.
* A signal name (e.g. ``RollingCounter``) -- skips just that signal,
  keeping the rest of whichever message(s) define it. Useful for a signal
  that's protocol noise (a rolling counter, a checksum) on a message that
  also carries real signals you want -- excluding the whole message would
  throw those away too.

Anything without the ``0x`` prefix is checked against message names first,
then signal names -- so there's no ambiguity between message- and
ID-based skipping. A token matching neither is reported as a warning
(``unknown_skip_names``), not silently ignored -- almost always a typo.
Skipped rows/signals get their own counters in the summary, separate from
"undecodable", and contribute no columns to ``decoded.csv`` at all (not
even blank ones). See the README for the recommended standard blacklist.

Needs ``cantools`` (``pip install ivision[decode]`` or plain ``pip install
cantools``) -- not a dependency of anything else in this project.

## Longitudinal acceleration, derived

The DBC has no direct longitudinal-accelerometer signal (GM's
``LateralAcceleration``, on ``EBCMVehicleDynamic``, is lateral only). Instead,
for each of ``EBCMWheelSpdFront`` and ``EBCMWheelSpdRear`` independently, this
averages that message's own left/right wheel speed pair into a
``vehicle_speed_mps`` value, then differentiates consecutive samples *of
that message alone* (not interleaved with any other message's rows -- using
each sample's own logged ``timestamp_ns``, not an assumed fixed rate) into
``longitudinal_accel_mps2``. Both names are introduced by *two* messages
(front and rear), so -- same rule as any other colliding signal name -- they
end up namespaced: ``EBCMWheelSpdFront.vehicle_speed_mps`` /
``EBCMWheelSpdFront.longitudinal_accel_mps2``, and the same pair for
``EBCMWheelSpdRear``. Kept as two
independent estimates -- front and rear are decoded from separate CAN
messages arriving at separate times, not synchronized to the same instant --
rather than merged into one signal, so they can be cross-checked against
each other: they should roughly agree if the DBC and this derivation are
both right. This is a plain finite difference, nothing smoothed -- real
speed samples are somewhat noisy discrete measurements, so treat the
derivative as a first, "does this look physically sane" pass, not a clean
signal. Filtering it is a real improvement to make later, once it's clear
the raw version isn't enough -- not something to build before that's
actually known.
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


def parse_skip(values) -> tuple:
    """Each value may be comma-separated. A token with an explicit '0x'
    prefix is an arbitration ID; anything else is a message name -- no
    guessing, so 'DEAD' (a plausible message name) is never mistaken for
    hex. Returns (skip_ids: set[int], skip_names: set[str])."""
    skip_ids: set = set()
    skip_names: set = set()
    for value in values or []:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lower().startswith("0x"):
                skip_ids.add(int(token, 16))
            else:
                skip_names.add(token)
    return skip_ids, skip_names


def decode_session(session_dir, dbc_path, out_path=None, skip_ids=None, skip_names=None) -> dict:
    """Decode <session_dir>/can.csv against dbc_path into one combined CSV.
    Returns a summary dict (also what the CLI prints).

    skip_names is classified against the loaded DBC: a token matching a
    message's own name skips that message entirely; a token matching a
    signal's name (defined on some message, but not itself a message name)
    skips just that signal, keeping the rest of whichever message(s) define
    it. A token matching neither is reported back as unknown_skip_names --
    likely a typo, not silently ignored."""
    db = load_database(dbc_path)
    session_dir = Path(session_dir)
    out_path = Path(out_path) if out_path is not None else session_dir / "decoded.csv"
    skip_ids = skip_ids or set()
    skip_names = skip_names or set()

    all_message_names = {m.name for m in db.messages}
    all_signal_names = {sig.name for m in db.messages for sig in m.signals}
    skip_message_names = skip_names & all_message_names
    skip_signal_names = skip_names - all_message_names
    unknown_skip_names = skip_signal_names - all_signal_names
    skip_signal_names -= unknown_skip_names

    # Each entry: [timestamp_ns, seq, message_name, {signal_name: value}].
    # Mutable in place (see _add_derived_longitudinal_accel) so derived
    # columns land in the same rows without a second read/write pass.
    decoded_rows: list = []
    signals_by_message: dict = {}
    undecodable: Counter = Counter()
    skipped: Counter = Counter()
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
            if can_id in skip_ids or message.name in skip_message_names:
                skipped[message.name] += 1
                continue
            data = bytes.fromhex(row["data"])
            try:
                signals = message.decode(data, allow_truncated=True)
            except Exception:
                undecodable[(can_id, is_extended)] += 1
                continue
            if skip_signal_names:
                signals = {k: v for k, v in signals.items() if k not in skip_signal_names}
            decoded_rows.append([int(row["timestamp_ns"]), int(row["seq"]), message.name, signals])
            signals_by_message.setdefault(message.name, set()).update(signals)

    decoded_rows.sort(key=lambda r: r[0])

    _add_derived_longitudinal_accel(decoded_rows, signals_by_message,
                                     "EBCMWheelSpdFront", "FLWheelSpd", "FRWheelSpd")
    _add_derived_longitudinal_accel(decoded_rows, signals_by_message,
                                     "EBCMWheelSpdRear", "RLWheelSpd", "RRWheelSpd")

    # A signal name gets the message-qualified column only if it would
    # otherwise collide with the same name from a different message; every
    # unambiguous signal keeps its plain name (see the module docstring).
    messages_per_name: dict = {}
    for msg, sigs in signals_by_message.items():
        for sig in sigs:
            messages_per_name.setdefault(sig, set()).add(msg)

    def column_name(msg: str, sig: str) -> str:
        return sig if len(messages_per_name[sig]) == 1 else f"{msg}.{sig}"

    columns = sorted({column_name(msg, sig) for msg in signals_by_message for sig in signals_by_message[msg]})

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ns", "seq", "message", *columns])
        # Forward-fill: each row is the full current state of every signal,
        # not just the ones this exact frame updated -- see the module
        # docstring. last_known persists across rows on purpose.
        last_known = {col: "" for col in columns}
        for timestamp_ns, seq, msg_name, signals in decoded_rows:
            for sig_name, value in signals.items():
                last_known[column_name(msg_name, sig_name)] = value
            writer.writerow([timestamp_ns, seq, msg_name, *(last_known[col] for col in columns)])

    decoded_counts = Counter(r[2] for r in decoded_rows)
    return {
        "total_rows": total_rows,
        "decoded_messages": dict(decoded_counts),
        "skipped_messages": dict(skipped),
        "skipped_signals": sorted(skip_signal_names),
        "unknown_skip_names": sorted(unknown_skip_names),
        "undecodable_ids": {f"0x{can_id:X}{'x' if ext else ''}": n
                            for (can_id, ext), n in undecodable.items()},
        "out_path": str(out_path),
    }


def _add_derived_longitudinal_accel(decoded_rows: list, signals_by_message: dict,
                                     message_name: str, left_signal: str, right_signal: str) -> None:
    """Mutates decoded_rows' signal dicts in place: for every row belonging to
    message_name, adds vehicle_speed_mps and longitudinal_accel_mps2 --
    computed from that message's own consecutive samples only, never
    interleaved with any other message's rows. A no-op if message_name was
    never decoded, or doesn't have both wheel-speed signals -- callable
    safely regardless of which messages a given session actually contains."""
    have = signals_by_message.get(message_name, set())
    if left_signal not in have or right_signal not in have:
        return
    subset = [r for r in decoded_rows if r[2] == message_name]  # same dict objects, not copies
    if not subset:
        return

    speeds_mps = [(r[3][left_signal] + r[3][right_signal]) / 2 / 3.6 for r in subset]
    subset[0][3]["vehicle_speed_mps"] = round(speeds_mps[0], 4)
    for i in range(1, len(subset)):
        subset[i][3]["vehicle_speed_mps"] = round(speeds_mps[i], 4)
        dt_s = (subset[i][0] - subset[i - 1][0]) / 1e9
        if dt_s > 0:
            subset[i][3]["longitudinal_accel_mps2"] = round((speeds_mps[i] - speeds_mps[i - 1]) / dt_s, 4)
    signals_by_message[message_name] |= {"vehicle_speed_mps", "longitudinal_accel_mps2"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", help="a session directory containing can.csv")
    parser.add_argument("--dbc", required=True, help="path to the .dbc signal definition file")
    parser.add_argument("--out", default=None, help="output CSV path (default: <session_dir>/decoded.csv)")
    parser.add_argument("--skip", action="append", default=None, metavar="ID_OR_NAME",
                        help="skip this message entirely -- '0x1E5' (arbitration ID, needs the 0x) "
                             "or 'VIN_Part1' (message name). Repeatable, or comma-separate several. A "
                             "token can also be a signal name (e.g. 'RollingCounter') -- skips just "
                             "that signal, keeping the rest of whichever message(s) define it. See "
                             "the README's standard command for the recommended default blacklist")
    args = parser.parse_args(argv)

    skip_ids, skip_names = parse_skip(args.skip)
    try:
        summary = decode_session(args.session_dir, args.dbc, out_path=args.out,
                                  skip_ids=skip_ids, skip_names=skip_names)
    except ImportError:
        print("cantools is not installed. Run: pip install cantools", file=sys.stderr)
        return 2

    print(f"total rows: {summary['total_rows']}")
    print(f"wrote {sum(summary['decoded_messages'].values())} decoded rows to {summary['out_path']}")
    print("decoded messages:")
    for name, n in sorted(summary["decoded_messages"].items()):
        print(f"  {name}: {n} rows")
    if summary["skipped_messages"]:
        skipped_total = sum(summary["skipped_messages"].values())
        print(f"skipped (blacklisted messages): {skipped_total} rows, {len(summary['skipped_messages'])} "
              f"messages: {', '.join(sorted(summary['skipped_messages']))}")
    if summary["skipped_signals"]:
        print(f"skipped (blacklisted signals, kept the rest of their message): "
              f"{', '.join(summary['skipped_signals'])}")
    if summary["unknown_skip_names"]:
        print(f"WARNING: --skip entries matched neither a message nor a signal name in this DBC "
              f"(typo?): {', '.join(summary['unknown_skip_names'])}", file=sys.stderr)
    if summary["undecodable_ids"]:
        undecoded_total = sum(summary["undecodable_ids"].values())
        print(f"undecodable (no DBC entry or decode failed): {undecoded_total} rows, "
              f"{len(summary['undecodable_ids'])} distinct IDs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

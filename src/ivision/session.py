"""Session directory and manifest, shared by every stream.

Layout::

    <base>/<session_id>/
        session.json     manifest (atomic rewrites)
        frames.csv       frame ledger
        frames/          one file per frame
        can.csv          CAN ledger, if a CAN recorder attached to this session

The manifest is written at start with ``status: "recording"`` and rewritten at
a clean stop with ``status: "complete"``. A manifest still saying "recording"
therefore means the session ended uncleanly (power cut, crash).

**Two independent processes can share one session** (e.g. ``frames.record``
and ``can.record`` recording the same drive): one calls ``Session.create()``,
the other calls ``Session.attach()`` on the same directory. Each stream only
ever writes its own ``streams[name]`` block (plus ``camera``, written only by
the frame recorder) -- ``flush()`` re-reads the on-disk manifest under a lock
and merges each of those independently before writing, so neither process's
stale in-memory copy of the *other's* data can clobber it. Both may call
``finalize()`` independently, in whatever order they actually stop in; each
finalize() naturally reflects "as of the last stream to stop" (see its
docstring). Don't add a new top-level manifest key without checking this
merge logic still holds -- it assumes each key is owned by exactly one
writer.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .clock import read_anchors

SCHEMA = "ivision.session/1"
MANIFEST_NAME = "session.json"


class Session:
    def __init__(self, root: Path, manifest: dict):
        self.root = Path(root)
        self.manifest = manifest
        # Which top-level keys THIS process has actually written since it was
        # created/attached -- not just carries around from an old snapshot.
        # flush() only ever overlays these onto the freshest disk copy; see
        # its docstring for why that distinction matters.
        self._touched_streams: set[str] = set()
        self._touched_camera = False

    @classmethod
    def create(cls, base_dir, camera: dict | None = None, session_id: str | None = None):
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        if session_id is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            # Random suffix: the Pi has no RTC, so wall-clock stamps can repeat.
            session_id = f"{stamp}-{secrets.token_hex(2)}"
        root = base / session_id
        root.mkdir()  # fails if it exists: never reuse a session directory
        (root / "frames").mkdir()

        manifest = {
            "schema": SCHEMA,
            "session_id": session_id,
            "status": "recording",
            "started": read_anchors(),
            "stopped": None,
            "camera": {"calibration": "uncalibrated", **(camera or {})},
            "software": {
                "ivision": __version__,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "streams": {},
        }
        session = cls(root, manifest)
        session.flush()
        return session

    @classmethod
    def attach(cls, root) -> "Session":
        """Join a session another process already created with ``create()``
        (e.g. a CAN recorder joining a frame session, or vice versa), so
        both streams' logs land in one folder. Does not create the
        directory or touch ``frames/`` -- the other process already did.
        Raises if no session exists there yet (create it first)."""
        root = Path(root)
        manifest = load_manifest(root)
        return cls(root, manifest)

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def frames_csv(self) -> Path:
        return self.root / "frames.csv"

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def set_stream(self, name: str, **fields) -> None:
        self.manifest["streams"].setdefault(name, {}).update(fields)
        self._touched_streams.add(name)

    def update_camera(self, **fields) -> None:
        """The only sanctioned way to change ``manifest["camera"]`` after
        creation (used by the frame recorder as it learns more about the
        real sensor config). Don't mutate ``session.manifest["camera"]``
        directly -- flush() needs ``_touched_camera`` to know this process
        actually owns the change, not just carries an old copy of it."""
        self.manifest["camera"].update(fields)
        self._touched_camera = True

    def finalize(self) -> None:
        self.manifest["status"] = "complete"
        self.manifest["stopped"] = read_anchors()
        self.flush()

    def flush(self) -> None:
        """Write the manifest atomically: readers see the old or new file.

        Safe when another process shares this session (see ``attach()``):
        under an exclusive lock, re-reads whatever's on disk right now and
        overlays only the ``streams`` keys and ``camera`` fields *this
        process has actually written* (``_touched_streams``,
        ``_touched_camera``) onto that fresh copy -- never the rest of its
        in-memory manifest, which for anything it hasn't touched is just a
        stale snapshot from whenever it last read the file (at ``create()``
        or ``attach()``). Overlaying the whole stale copy instead of just
        the touched keys was tried first and is exactly the bug this
        docstring is warning you not to reintroduce: a second stream
        finishing later would silently revert the first stream's already
        -written "complete" status back to its own stale "recording" copy.
        Other top-level keys (``status``, ``stopped``) are simply this
        process's value -- safe because they're only ever set once each, by
        ``create()`` and ``finalize()`` respectively, and finalize() calls
        naturally land in real chronological stop order.
        """
        lock_fd = os.open(self.root / (MANIFEST_NAME + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if self.manifest_path.exists():
                on_disk = load_manifest(self.root)
                merged = dict(on_disk)
                merged["camera"] = dict(on_disk.get("camera", {}))
                if self._touched_camera:
                    merged["camera"].update(self.manifest.get("camera", {}))
                merged["streams"] = dict(on_disk.get("streams", {}))
                for name in self._touched_streams:
                    merged["streams"][name] = self.manifest["streams"].get(name, {})
                for key, value in self.manifest.items():
                    if key not in ("camera", "streams"):
                        merged[key] = value
                self.manifest = merged
            self._write(self.manifest)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _write(self, manifest: dict) -> None:
        tmp = self.root / (MANIFEST_NAME + ".tmp")
        # default=str: an odd value from camera metadata must never crash a recording.
        data = json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.manifest_path)
        try:
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # directory fsync is best-effort (not supported everywhere)


def load_manifest(session_dir) -> dict:
    with open(Path(session_dir) / MANIFEST_NAME, encoding="utf-8") as f:
        return json.load(f)

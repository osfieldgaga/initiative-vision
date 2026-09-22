"""Session directory and manifest, shared by every stream.

Layout::

    <base>/<session_id>/
        session.json     manifest (atomic rewrites)
        frames.csv       frame ledger
        frames/          one file per frame

The manifest is written at start with ``status: "recording"`` and rewritten at
a clean stop with ``status: "complete"``. A manifest still saying "recording"
therefore means the session ended uncleanly (power cut, crash).
"""

from __future__ import annotations

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

    def finalize(self) -> None:
        self.manifest["status"] = "complete"
        self.manifest["stopped"] = read_anchors()
        self.flush()

    def flush(self) -> None:
        """Write the manifest atomically: readers see the old or new file."""
        tmp = self.root / (MANIFEST_NAME + ".tmp")
        # default=str: an odd value from camera metadata must never crash a recording.
        data = json.dumps(self.manifest, indent=2, sort_keys=True, default=str) + "\n"
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

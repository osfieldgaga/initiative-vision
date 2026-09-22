"""JPEG encoding for camera frames.

Picamera2's ``RGB888`` format gives arrays in OpenCV order: memory order is
B, G, R, shape (height, width, 3). Both encoders below are told this. If saved
images ever show red and blue swapped, this is the place to fix (a real frame
from the Pi was checked on 2026-09-21: a brown wooden door came out brown).

Which encoder: ``simplejpeg`` (a thin wrapper on libjpeg-turbo) is the default
when installed. Measured on the Pi 3B at 1640x1232 with ``encode_bench``:

    encoder     1 thread   2 threads   4 threads
    pillow       10.5 fps    12.6 fps    14.2 fps   (threads barely help)
    simplejpeg   18.0 fps    35.7 fps    67.2 fps   (scales with cores)

I first assumed Pillow's threads would scale; they don't, and on the Pi two
Pillow encoder threads dragged capture down to 11 fps. Pillow stays as the
fallback so a machine without simplejpeg still works.
"""

from __future__ import annotations

import io

VALID_ROTATIONS = (0, 90, 180, 270)


def rotate_cw(array, degrees: int):
    """Rotate an (h, w, 3) array clockwise by 0/90/180/270 degrees.

    Exact — a transpose and/or axis flip, no interpolation. Added 2026-09-22:
    the Pi's ISP can't do this in hardware (``rpicam-hello --rotation 90``
    errors with "transforms requiring transpose not supported" on this board).

    NOT cheap on the Pi 3B for 90/270, despite what an earlier version of this
    docstring claimed. Measured on a real 1280x1280 frame: creating the
    rotated view is free (~0.1 ms), but 90/270 leaves it non-contiguous, and
    materializing that back to contiguous memory for the encoder
    (``np.ascontiguousarray``) cost 130 ms/frame — over 3x the ~41 ms the JPEG
    encode itself took. That's a cache-hostile strided-transpose cost
    intrinsic to the Pi 3B's memory subsystem, not this function's algorithm.
    It took a clean-running recorder (2-3 encoder threads, 0-7% drops) to 80%+
    dropped frames. 180 only flips, no transpose, and was not separately
    measured — its cost is unknown, so don't assume it's safe either.

    Decision (2026-09-22): rotation is done in Phase 2 (AWS) instead, where
    compute isn't the constraint it is here. This function is kept for
    completeness and other hardware, but --rotate should not be used for
    recording on the Pi 3B as things stand.
    """
    if degrees not in VALID_ROTATIONS:
        raise ValueError(f"rotation must be one of {VALID_ROTATIONS}, got {degrees}")
    if degrees == 0:
        return array
    import numpy as np

    # np.rot90's k turns counter-clockwise; negate for a clockwise degrees value.
    return np.rot90(array, k=-(degrees // 90) % 4)


def encode_jpeg_bgr(array, quality: int = 90) -> bytes:
    """Pillow encoder (the fallback). Reads the bytes as raw BGR."""
    import numpy as np
    from PIL import Image

    # A frame with padded rows is a non-contiguous view; this copies only then.
    arr = np.ascontiguousarray(array)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected an (h, w, 3) array, got shape {arr.shape}")
    height, width = arr.shape[:2]
    image = Image.frombuffer("RGB", (width, height), arr, "raw", "BGR", 0, 1)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def encode_jpeg_simplejpeg(array, quality: int = 90) -> bytes:
    """simplejpeg encoder (the default). 4:2:0 chroma, fast DCT."""
    import numpy as np
    import simplejpeg

    arr = np.ascontiguousarray(array)  # copies only if rows are padded
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected an (h, w, 3) array, got shape {arr.shape}")
    return simplejpeg.encode_jpeg(
        arr, quality=quality, colorspace="BGR", colorsubsampling="420", fastdct=True
    )


def pick_encoder(preference: str = "auto"):
    """Return ``(name, function)``. ``auto`` = simplejpeg if importable, else Pillow."""
    if preference not in ("auto", "simplejpeg", "pillow"):
        raise ValueError(f"unknown encoder {preference!r}; use auto, simplejpeg or pillow")
    if preference in ("auto", "simplejpeg"):
        try:
            import simplejpeg  # noqa: F401

            return "simplejpeg", encode_jpeg_simplejpeg
        except ImportError:
            if preference == "simplejpeg":
                raise ImportError(
                    "simplejpeg is not installed. On the Pi: sudo apt install -y python3-simplejpeg"
                ) from None
    return "pillow", encode_jpeg_bgr

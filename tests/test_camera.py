"""Camera frame helpers (no ffmpeg required)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.camera.service import frame_difference  # noqa: E402


def _jpeg(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def test_difference_identical_frames() -> None:
    frame = _jpeg((20, 20, 20))
    assert frame_difference(frame, frame) < 0.01


def test_difference_scene_change() -> None:
    black = _jpeg((0, 0, 0))
    white = _jpeg((255, 255, 255))
    assert frame_difference(black, white) > 0.9


def test_difference_partial_change() -> None:
    first = Image.new("RGB", (320, 180), (0, 0, 0))
    second = Image.new("RGB", (320, 180), (0, 0, 0))
    for x in range(160, 320):
        for y in range(0, 180):
            second.putpixel((x, y), (255, 255, 255))
    one, two = io.BytesIO(), io.BytesIO()
    first.save(one, format="JPEG", quality=90)
    second.save(two, format="JPEG", quality=90)
    share = frame_difference(one.getvalue(), two.getvalue())
    assert 0.3 < share < 0.7

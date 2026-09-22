import io
import unittest

import numpy as np
from PIL import Image

from ivision.frames.encode import encode_jpeg_bgr, encode_jpeg_simplejpeg, pick_encoder, rotate_cw

try:
    import simplejpeg  # noqa: F401
    HAVE_SIMPLEJPEG = True
except ImportError:
    HAVE_SIMPLEJPEG = False


def decode(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


class EncodeTest(unittest.TestCase):
    def test_output_is_a_decodable_jpeg_of_the_right_size(self):
        arr = np.zeros((48, 64, 3), np.uint8)
        data = encode_jpeg_bgr(arr, quality=90)
        self.assertEqual(data[:2], b"\xff\xd8")
        self.assertEqual(data[-2:], b"\xff\xd9")
        self.assertEqual(decode(data).size, (64, 48))  # PIL size is (width, height)

    def test_channel_order_is_bgr_in_memory(self):
        # Picamera2 RGB888 arrays are B,G,R in memory. A pure red pixel must
        # come out red, not blue.
        arr = np.zeros((32, 32, 3), np.uint8)
        arr[:, :, 2] = 255  # index 2 = R
        r, g, b = decode(encode_jpeg_bgr(arr, 95)).getpixel((16, 16))
        self.assertGreater(r, 240)
        self.assertLess(g, 20)
        self.assertLess(b, 20)

    def test_padded_rows_view_gives_same_image_as_contiguous(self):
        # libcamera rows can be padded, which shows up as a non-contiguous view.
        rng = np.random.default_rng(0)
        padded = rng.integers(0, 255, (40, 64 + 16, 3), dtype=np.uint8)
        view = padded[:, :64, :]
        self.assertFalse(view.flags.c_contiguous)
        self.assertEqual(encode_jpeg_bgr(view, 90), encode_jpeg_bgr(view.copy(), 90))

    def test_quality_changes_size(self):
        rng = np.random.default_rng(1)
        arr = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
        self.assertGreater(len(encode_jpeg_bgr(arr, 95)), len(encode_jpeg_bgr(arr, 20)))

    def test_rejects_wrong_shape(self):
        with self.assertRaises(ValueError):
            encode_jpeg_bgr(np.zeros((10, 10), np.uint8))
        with self.assertRaises(ValueError):
            encode_jpeg_bgr(np.zeros((10, 10, 4), np.uint8))


@unittest.skipUnless(HAVE_SIMPLEJPEG, "simplejpeg not installed here")
class SimplejpegTest(unittest.TestCase):
    def test_decodable_and_right_size(self):
        data = encode_jpeg_simplejpeg(np.zeros((48, 64, 3), np.uint8), 90)
        self.assertEqual(data[:2], b"\xff\xd8")
        self.assertEqual(data[-2:], b"\xff\xd9")
        self.assertEqual(decode(data).size, (64, 48))

    def test_channel_order_matches_bgr_memory_layout(self):
        # The important one: a red pixel (B=0,G=0,R=255 in memory) must stay red.
        arr = np.zeros((32, 32, 3), np.uint8)
        arr[:, :, 2] = 255
        r, g, b = decode(encode_jpeg_simplejpeg(arr, 95)).getpixel((16, 16))
        self.assertGreater(r, 240)
        self.assertLess(g, 20)
        self.assertLess(b, 20)
        arr = np.zeros((32, 32, 3), np.uint8)
        arr[:, :, 0] = 255  # blue
        r, g, b = decode(encode_jpeg_simplejpeg(arr, 95)).getpixel((16, 16))
        self.assertGreater(b, 240)
        self.assertLess(r, 20)

    def test_colours_agree_with_the_pillow_encoder_on_a_busy_image(self):
        rng = np.random.default_rng(3)
        base = rng.integers(0, 255, (60, 80, 1), dtype=np.uint8)
        arr = np.concatenate([base, 255 - base, np.roll(base, 5, axis=1)], axis=2)
        a = np.asarray(decode(encode_jpeg_simplejpeg(arr, 95)), dtype=float)
        b = np.asarray(decode(encode_jpeg_bgr(arr, 95)), dtype=float)
        self.assertLess(np.abs(a - b).mean(), 8)  # same picture, small codec differences

    def test_padded_rows_view(self):
        rng = np.random.default_rng(0)
        padded = rng.integers(0, 255, (40, 64 + 16, 3), dtype=np.uint8)
        view = padded[:, :64, :]
        self.assertEqual(encode_jpeg_simplejpeg(view, 90), encode_jpeg_simplejpeg(view.copy(), 90))

    def test_rejects_wrong_shape(self):
        with self.assertRaises(ValueError):
            encode_jpeg_simplejpeg(np.zeros((10, 10), np.uint8))


class PickEncoderTest(unittest.TestCase):
    def test_auto_prefers_simplejpeg_when_installed(self):
        name, fn = pick_encoder("auto")
        self.assertEqual(name, "simplejpeg" if HAVE_SIMPLEJPEG else "pillow")

    def test_pillow_can_be_forced(self):
        self.assertEqual(pick_encoder("pillow"), ("pillow", encode_jpeg_bgr))

    def test_auto_falls_back_to_pillow_and_explicit_simplejpeg_explains(self):
        from unittest import mock

        with mock.patch.dict("sys.modules", {"simplejpeg": None}):  # import raises ImportError
            self.assertEqual(pick_encoder("auto")[0], "pillow")
            with self.assertRaisesRegex(ImportError, "python3-simplejpeg"):
                pick_encoder("simplejpeg")

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            pick_encoder("gif")


class RotateCwTest(unittest.TestCase):
    # (h=2, w=3) with a distinct label at each position, so a rotation's exact effect
    # on every pixel (not just shape) can be checked, not merely inferred.
    ORIGINAL = np.array([[1, 2, 3], [4, 5, 6]])

    def test_zero_degrees_is_a_true_no_op(self):
        out = rotate_cw(self.ORIGINAL, 0)
        self.assertIs(out, self.ORIGINAL)  # not even a copy

    def test_90_is_a_genuine_clockwise_rotation(self):
        # Verified by hand (2026-09-22): physically rotating a rectangle clockwise cycles
        # its edges top->right->bottom->left, so corner-by-corner: TL->TR, TR->BR,
        # BR->BL, BL->TL. This checks every element, matching that exactly.
        out = rotate_cw(self.ORIGINAL, 90)
        self.assertEqual(out.shape, (3, 2))  # h and w swap
        np.testing.assert_array_equal(out, [[4, 1], [5, 2], [6, 3]])

    def test_180_reverses_both_axes_shape_unchanged(self):
        out = rotate_cw(self.ORIGINAL, 180)
        self.assertEqual(out.shape, (2, 3))
        np.testing.assert_array_equal(out, [[6, 5, 4], [3, 2, 1]])

    def test_270_is_the_inverse_of_90(self):
        out = rotate_cw(self.ORIGINAL, 270)
        self.assertEqual(out.shape, (3, 2))
        np.testing.assert_array_equal(out, [[3, 6], [2, 5], [1, 4]])
        # Rotating 90 then 270 (or vice versa) must return to the original.
        np.testing.assert_array_equal(rotate_cw(rotate_cw(self.ORIGINAL, 90), 270), self.ORIGINAL)

    def test_four_quarter_turns_return_to_the_original(self):
        out = self.ORIGINAL
        for _ in range(4):
            out = rotate_cw(out, 90)
        np.testing.assert_array_equal(out, self.ORIGINAL)

    def test_works_on_a_real_h_w_3_frame_and_round_trips_through_jpeg(self):
        rng = np.random.default_rng(0)
        arr = rng.integers(0, 255, (48, 64, 3), dtype=np.uint8)  # h=48, w=64
        rotated = rotate_cw(arr, 90)
        self.assertEqual(rotated.shape, (64, 48, 3))
        data = encode_jpeg_bgr(rotated, 90)
        self.assertEqual(decode(data).size, (48, 64))  # PIL size is (width, height)

    def test_rejects_a_degree_value_that_is_not_a_multiple_of_90(self):
        with self.assertRaises(ValueError):
            rotate_cw(self.ORIGINAL, 45)
        with self.assertRaises(ValueError):
            rotate_cw(self.ORIGINAL, 360)  # not one of the four canonical values


if __name__ == "__main__":
    unittest.main()

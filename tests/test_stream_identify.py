"""Read-only MJPEG camera identification contracts using synthetic JPEG data."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from cc2camera import stream_identify


class StreamIdentifyTests(unittest.TestCase):
    def test_known_30b_fingerprint_requires_complete_signature(self):
        known = stream_identify.JpegFingerprint(
            width=640,
            height=360,
            sampling=((1, 2, 2), (2, 1, 1), (3, 1, 1)),
            marker_sequence=("DQT", "DQT", "SOF0", "DHT", "DHT", "DHT", "DHT", "SOS"),
            dqt_sha256="13660d69eacf5a054bdf3d88d8aead55e5857f704b6f3d9fa098ca93ecf9c44b",
            jfif=None,
            restart_interval=None,
            restart_markers=0,
        )
        self.assertEqual(stream_identify.classify_fingerprint(known), "EF-S7-V1.0.30B")
        changed = stream_identify.JpegFingerprint(
            **{**known.__dict__, "restart_markers": 1}
        )
        self.assertIsNone(stream_identify.classify_fingerprint(changed))

    def test_known_30d_fingerprint_requires_complete_signature(self):
        known = stream_identify.JpegFingerprint(
            width=640,
            height=360,
            sampling=((1, 2, 2), (2, 1, 1), (3, 1, 1)),
            marker_sequence=(
                "APP0", "DQT", "DQT", "SOF0", "DRI",
                "DHT", "DHT", "DHT", "DHT", "SOS",
            ),
            dqt_sha256="2d13c72678b9293bb85e06153666f0b383896859603f8dc6f0cee57e816efec1",
            jfif=(1, 2, 1, 72, 72),
            restart_interval=40,
            restart_markers=22,
        )
        self.assertEqual(stream_identify.classify_fingerprint(known), "EF-S7-V1.0.30D")
        changed = stream_identify.JpegFingerprint(
            **{**known.__dict__, "restart_interval": 41}
        )
        self.assertIsNone(stream_identify.classify_fingerprint(changed))

    def test_multipart_reader_is_bounded_and_requires_jpeg_parts(self):
        jpeg = b"\xff\xd8synthetic\xff\xd9"
        payload = (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpeg)).encode()
            + b"\r\n\r\n"
            + jpeg
            + b"\r\n"
        ) * 3
        self.assertEqual(
            stream_identify.read_mjpeg_frames(io.BytesIO(payload), b"frame", 3),
            [jpeg, jpeg, jpeg],
        )

        bad = b"--frame\r\nContent-Type: text/plain\r\nContent-Length: 1\r\n\r\nx"
        with self.assertRaisesRegex(stream_identify.StreamIdentificationError, "image/jpeg"):
            stream_identify.read_mjpeg_frames(io.BytesIO(bad), b"frame", 1)

    def test_multipart_reader_rejects_short_or_oversized_frames(self):
        short = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\nabc"
        with self.assertRaisesRegex(stream_identify.StreamIdentificationError, "inside a JPEG"):
            stream_identify.read_mjpeg_frames(io.BytesIO(short), b"frame", 1)

        large = (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(stream_identify.MAX_FRAME_BYTES + 1).encode()
            + b"\r\n\r\n"
        )
        with self.assertRaisesRegex(stream_identify.StreamIdentificationError, "accepted bound"):
            stream_identify.read_mjpeg_frames(io.BytesIO(large), b"frame", 1)

    def test_printer_address_does_not_accept_paths_or_ports(self):
        self.assertEqual(stream_identify._stream_url("192.0.2.10"), "http://192.0.2.10:8080/")
        self.assertEqual(stream_identify._stream_url("camera.local"), "http://camera.local:8080/")
        self.assertEqual(stream_identify._stream_url("2001:db8::1"), "http://[2001:db8::1]:8080/")
        for bad in ("http://camera.local", "camera.local/path", "camera.local:9000", ""):
            with self.subTest(bad=bad), self.assertRaises(stream_identify.StreamIdentificationError):
                stream_identify._stream_url(bad)

    def test_inconsistent_frames_fail_closed_as_unknown(self):
        first = stream_identify.JpegFingerprint(
            640, 360, ((1, 2, 2), (2, 1, 1), (3, 1, 1)),
            ("DQT", "DQT", "SOF0", "DHT", "DHT", "DHT", "DHT", "SOS"),
            "13660d69eacf5a054bdf3d88d8aead55e5857f704b6f3d9fa098ca93ecf9c44b",
            None, None, 0,
        )
        second = stream_identify.JpegFingerprint(
            **{**first.__dict__, "dqt_sha256": "0" * 64}
        )
        with mock.patch.object(
            stream_identify, "fingerprint_jpeg", side_effect=[first, second]
        ):
            result = stream_identify.identify_frames([b"one", b"two"])
        self.assertIsNone(result.family)
        self.assertEqual(result.frames_checked, 2)


if __name__ == "__main__":
    unittest.main()

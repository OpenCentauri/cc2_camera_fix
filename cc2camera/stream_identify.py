"""Read-only identification of known CC2 camera families from their MJPEG stream."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
from typing import BinaryIO, Callable
from urllib.request import urlopen


DEFAULT_STREAM_PORT = 8080
DEFAULT_TIMEOUT = 5.0
IDENTIFICATION_FRAMES = 3
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_MULTIPART_LINE = 8192
MAX_MULTIPART_HEADERS = 32 * 1024
MAX_PREAMBLE_BYTES = 32 * 1024

# Observed on two independent EF-S7-V1.0.30B cameras.
_DQT_30B_SHA256 = "13660d69eacf5a054bdf3d88d8aead55e5857f704b6f3d9fa098ca93ecf9c44b"
# Observed on one EF-S7-V1.0.30D camera.
_DQT_30D_SHA256 = "2d13c72678b9293bb85e06153666f0b383896859603f8dc6f0cee57e816efec1"

_MARKERS_30B = ("DQT", "DQT", "SOF0", "DHT", "DHT", "DHT", "DHT", "SOS")
_MARKERS_30D = (
    "APP0", "DQT", "DQT", "SOF0", "DRI", "DHT", "DHT", "DHT", "DHT", "SOS"
)


class StreamIdentificationError(ValueError):
    """The camera stream could not be safely parsed for identification."""


@dataclass(frozen=True)
class JpegFingerprint:
    width: int
    height: int
    sampling: tuple[tuple[int, int, int], ...]
    marker_sequence: tuple[str, ...]
    dqt_sha256: str
    jfif: tuple[int, int, int, int, int] | None
    restart_interval: int | None
    restart_markers: int


@dataclass(frozen=True)
class CameraIdentification:
    family: str | None
    fingerprint: JpegFingerprint
    frames_checked: int


def _marker_name(marker: int) -> str:
    names = {
        0xC0: "SOF0",
        0xC2: "SOF2",
        0xC4: "DHT",
        0xD8: "SOI",
        0xD9: "EOI",
        0xDA: "SOS",
        0xDB: "DQT",
        0xDD: "DRI",
        0xE0: "APP0",
    }
    if 0xE1 <= marker <= 0xEF:
        return f"APP{marker - 0xE0}"
    return names.get(marker, f"0xFF{marker:02X}")


def fingerprint_jpeg(frame: bytes) -> JpegFingerprint:
    if len(frame) < 4 or not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        raise StreamIdentificationError("stream frame is not a complete JPEG")

    pos = 2
    marker_sequence: list[str] = []
    dqt_payloads: list[bytes] = []
    width = height = 0
    sampling: tuple[tuple[int, int, int], ...] = ()
    jfif: tuple[int, int, int, int, int] | None = None
    restart_interval: int | None = None
    scan_start: int | None = None

    while pos < len(frame) - 2:
        if frame[pos] != 0xFF:
            raise StreamIdentificationError("invalid JPEG marker structure before scan data")
        while pos < len(frame) and frame[pos] == 0xFF:
            pos += 1
        if pos >= len(frame):
            raise StreamIdentificationError("truncated JPEG marker")
        marker = frame[pos]
        pos += 1
        if marker in range(0xD0, 0xD8) or marker == 0x01:
            marker_sequence.append(_marker_name(marker))
            continue
        if marker == 0xD9:
            raise StreamIdentificationError("JPEG ended before a scan")
        if pos + 2 > len(frame):
            raise StreamIdentificationError("truncated JPEG segment length")
        length = int.from_bytes(frame[pos : pos + 2], "big")
        if length < 2 or pos + length > len(frame):
            raise StreamIdentificationError("invalid JPEG segment length")
        payload = frame[pos + 2 : pos + length]
        name = _marker_name(marker)
        marker_sequence.append(name)
        pos += length

        if marker == 0xDB:
            dqt_payloads.append(payload)
        elif marker == 0xC0:
            if len(payload) < 6:
                raise StreamIdentificationError("truncated baseline JPEG frame header")
            height = int.from_bytes(payload[1:3], "big")
            width = int.from_bytes(payload[3:5], "big")
            components = payload[5]
            if len(payload) != 6 + components * 3:
                raise StreamIdentificationError("invalid baseline JPEG component table")
            sampling = tuple(
                (payload[6 + i * 3], payload[7 + i * 3] >> 4, payload[7 + i * 3] & 0x0F)
                for i in range(components)
            )
        elif marker == 0xE0 and payload.startswith(b"JFIF\0") and len(payload) >= 12:
            jfif = (
                payload[5],
                payload[6],
                payload[7],
                int.from_bytes(payload[8:10], "big"),
                int.from_bytes(payload[10:12], "big"),
            )
        elif marker == 0xDD:
            if len(payload) != 2:
                raise StreamIdentificationError("invalid JPEG restart interval")
            restart_interval = int.from_bytes(payload, "big")
        elif marker == 0xDA:
            scan_start = pos
            break

    if scan_start is None or not width or not height or not dqt_payloads:
        raise StreamIdentificationError("JPEG lacks required baseline encoder metadata")
    scan = frame[scan_start:-2]
    restart_markers = sum(
        scan.count(bytes((0xFF, marker))) for marker in range(0xD0, 0xD8)
    )
    return JpegFingerprint(
        width=width,
        height=height,
        sampling=sampling,
        marker_sequence=tuple(marker_sequence),
        dqt_sha256=hashlib.sha256(b"".join(dqt_payloads)).hexdigest(),
        jfif=jfif,
        restart_interval=restart_interval,
        restart_markers=restart_markers,
    )


def classify_fingerprint(fingerprint: JpegFingerprint) -> str | None:
    common = (
        fingerprint.width == 640
        and fingerprint.height == 360
        and fingerprint.sampling == ((1, 2, 2), (2, 1, 1), (3, 1, 1))
    )
    if not common:
        return None
    if (
        fingerprint.marker_sequence == _MARKERS_30B
        and fingerprint.dqt_sha256 == _DQT_30B_SHA256
        and fingerprint.jfif is None
        and fingerprint.restart_interval is None
        and fingerprint.restart_markers == 0
    ):
        return "EF-S7-V1.0.30B"
    if (
        fingerprint.marker_sequence == _MARKERS_30D
        and fingerprint.dqt_sha256 == _DQT_30D_SHA256
        and fingerprint.jfif == (1, 2, 1, 72, 72)
        and fingerprint.restart_interval == 40
        and fingerprint.restart_markers == 22
    ):
        return "EF-S7-V1.0.30D"
    return None


def identify_frames(frames: list[bytes]) -> CameraIdentification:
    if not frames:
        raise StreamIdentificationError("camera stream contained no JPEG frames")
    fingerprints = [fingerprint_jpeg(frame) for frame in frames]
    first = fingerprints[0]
    if any(item != first for item in fingerprints[1:]):
        return CameraIdentification(None, first, len(fingerprints))
    return CameraIdentification(classify_fingerprint(first), first, len(fingerprints))


def _readline(stream: BinaryIO) -> bytes:
    line = stream.readline(MAX_MULTIPART_LINE + 1)
    if len(line) > MAX_MULTIPART_LINE:
        raise StreamIdentificationError("MJPEG multipart line is too long")
    return line


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise StreamIdentificationError("MJPEG stream ended inside a JPEG frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_mjpeg_frames(stream: BinaryIO, boundary: bytes, count: int) -> list[bytes]:
    delimiter = b"--" + boundary
    closing = delimiter + b"--"
    frames: list[bytes] = []
    preamble = 0

    while len(frames) < count:
        line = _readline(stream)
        if not line:
            break
        stripped = line.rstrip(b"\r\n")
        if stripped == closing:
            break
        if stripped != delimiter:
            preamble += len(line)
            if preamble > MAX_PREAMBLE_BYTES:
                raise StreamIdentificationError("MJPEG multipart boundary was not found")
            continue

        header_bytes = 0
        headers: dict[bytes, bytes] = {}
        while True:
            line = _readline(stream)
            if not line:
                raise StreamIdentificationError("MJPEG stream ended inside multipart headers")
            header_bytes += len(line)
            if header_bytes > MAX_MULTIPART_HEADERS:
                raise StreamIdentificationError("MJPEG multipart headers are too large")
            if line in (b"\r\n", b"\n"):
                break
            if b":" not in line:
                raise StreamIdentificationError("invalid MJPEG multipart header")
            name, value = line.split(b":", 1)
            headers[name.strip().lower()] = value.strip()

        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if content_type != b"image/jpeg":
            raise StreamIdentificationError("MJPEG part is not image/jpeg")
        raw_length = headers.get(b"content-length")
        if raw_length is None:
            raise StreamIdentificationError("MJPEG part has no Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise StreamIdentificationError("invalid MJPEG Content-Length") from exc
        if not 0 < length <= MAX_FRAME_BYTES:
            raise StreamIdentificationError("MJPEG frame length is outside the accepted bound")
        frames.append(_read_exact(stream, length))

    if len(frames) < count:
        raise StreamIdentificationError(
            f"MJPEG stream ended after {len(frames)} frame(s); {count} are required"
        )
    return frames


def _stream_url(host: str, port: int = DEFAULT_STREAM_PORT) -> str:
    value = host.strip()
    if not value or any(ch.isspace() for ch in value) or any(ch in value for ch in "/?#@"):
        raise StreamIdentificationError(
            "printer address must be a hostname or IP address, without a URL path"
        )
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError as exc:
            raise StreamIdentificationError(
                "printer address must not include a port; port 8080 is used"
            ) from exc
        value = f"[{value}]"
    return f"http://{value}:{port}/"


def identify_camera_stream(
    host: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[..., object] = urlopen,
) -> CameraIdentification:
    url = _stream_url(host)
    try:
        response = opener(url, timeout=timeout)
    except OSError as exc:
        raise StreamIdentificationError(
            f"could not open camera stream at {url}: {exc}"
        ) from exc
    with response:
        content_type = response.headers.get_content_type()
        boundary = response.headers.get_param("boundary")
        if content_type != "multipart/x-mixed-replace" or not boundary:
            raise StreamIdentificationError(
                "camera endpoint is not an MJPEG multipart stream"
            )
        if isinstance(boundary, str):
            boundary_bytes = boundary.encode("ascii", "strict")
        else:
            boundary_bytes = bytes(boundary)
        frames = read_mjpeg_frames(response, boundary_bytes, IDENTIFICATION_FRAMES)
    return identify_frames(frames)

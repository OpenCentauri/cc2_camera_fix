"""HID transport for the two CC2 updater stages.

No module import opens a device.  Every write is reachable only through an
explicit method whose name describes the transition or restore operation.
"""

from __future__ import annotations

import time
from typing import Callable

from .commands import (
    NormalCommand,
    build_boot_metadata_request,
    build_upgrade_request,
    build_upload_commit_request,
    build_upload_data_request,
    build_upload_initialize_request,
    build_upload_target_request,
    decode_boot_ack,
    decode_boot_terminal_status,
)

from .protocol import (
    BOOT_HID_PID,
    BOOT_REPORT_SIZE,
    BOOT_VID,
    NORMAL_PID,
    NORMAL_REPORT_SIZE,
    NORMAL_VID,
    NormalFrame,
    ProtocolError,
    UpdatePlan,
    from_boot_hid_report,
    iter_data_frames,
    parse_normal_report,
    to_boot_hid_report,
)


ADB_STARTUP_PATH = "/etc/conf.d/system.sh"
ADB_STARTUP_CONTENT = b"/bin/adbd &"
NORMAL_UPLOAD_CHUNK_SIZE = NORMAL_REPORT_SIZE - 14
NORMAL_UPLOAD_CAPACITY = 0x01000200


def _hid_module():
    try:
        import hid  # type: ignore
    except ImportError as exc:
        raise ProtocolError(
            "USB support requires the optional 'hidapi' Python package"
        ) from exc
    return hid


def enumerate_hid() -> list[dict]:
    hid = _hid_module()
    return list(hid.enumerate())


def expected_devices() -> list[dict]:
    return [
        item
        for item in enumerate_hid()
        if (item.get("vendor_id"), item.get("product_id"))
        in ((NORMAL_VID, NORMAL_PID), (BOOT_VID, BOOT_HID_PID))
    ]


class HidHandle:
    def __init__(self, vid: int, pid: int):
        hid = _hid_module()
        matches = hid.enumerate(vid, pid)
        if len(matches) != 1:
            raise ProtocolError(
                f"expected exactly one HID {vid:04x}:{pid:04x}, found {len(matches)}"
            )
        self.info = matches[0]
        self.device = hid.device()
        self.device.open_path(self.info["path"])

    def close(self) -> None:
        self.device.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def write_exact(self, report: bytes) -> None:
        count = self.device.write(report)
        if count not in (len(report), len(report) - 1):
            raise ProtocolError(f"short HID write: {count} of {len(report)} bytes")

    def read(self, size: int, timeout_ms: int) -> bytes:
        data = bytes(self.device.read(size, timeout_ms))
        if not data:
            raise ProtocolError("timed out waiting for HID response")
        return data


def exchange_normal(
    handle: HidHandle,
    request: bytes,
    *,
    timeout_ms: int = 5000,
) -> NormalFrame:
    """Send one already-built request and require an echoed success reply.

    This is the public transport counterpart to :mod:`cc2flash.commands`.
    Keeping construction separate makes the complete command surface available
    to library users without performing I/O at import/build time.  The function
    cannot make privileged SET/upload/upgrade reports safe; callers must inspect
    the selected command's documented side effects before sending it.
    """

    sent = parse_normal_report(request)
    handle.write_exact(request)
    response = parse_normal_report(handle.read(NORMAL_REPORT_SIZE, timeout_ms))
    if response.command != sent.command:
        raise ProtocolError(
            f"normal updater replied to 0x{sent.command:04x} with 0x{response.command:04x}"
        )
    if response.status != 0:
        raise ProtocolError(
            f"normal updater rejected command 0x{sent.command:04x}, status={response.status}"
        )
    return response


def upload_normal_file(path: str, content: bytes) -> None:
    """Overwrite one file through Linux ``hid_update``'s 0x3xxx protocol.

    This is intentionally a narrowly validated producer for the recovered,
    unauthenticated uploader. The target is removed before being recreated and
    chmodded 0777 by the device-side commit handler.
    """

    if not content:
        raise ProtocolError("normal upload content must not be empty")
    if len(content) > NORMAL_UPLOAD_CAPACITY:
        raise ProtocolError("normal upload exceeds the device's receive buffer")

    # Build target selection before opening USB so path validation cannot leave
    # the device halfway through the upload state machine.
    initialize_request = build_upload_initialize_request()
    target_request = build_upload_target_request(NormalCommand.UPLOAD_TARGET_3110, path)

    with HidHandle(NORMAL_VID, NORMAL_PID) as handle:
        exchange_normal(handle, initialize_request)
        exchange_normal(handle, target_request)
        chunks = [
            content[start : start + NORMAL_UPLOAD_CHUNK_SIZE]
            for start in range(0, len(content), NORMAL_UPLOAD_CHUNK_SIZE)
        ]
        for sequence, chunk in enumerate(chunks):
            final = sequence + 1 == len(chunks)
            exchange_normal(
                handle,
                build_upload_data_request(sequence, chunk, final=final),
            )
        exchange_normal(handle, build_upload_commit_request())


def install_adb_startup() -> None:
    """Install the exact boot hook that starts the existing root ``adbd``."""

    upload_normal_file(ADB_STARTUP_PATH, ADB_STARTUP_CONTENT)


def enter_bootloader() -> None:
    """Write the two upgrade flags through normal Linux hid_update."""

    request = build_upgrade_request()
    with HidHandle(NORMAL_VID, NORMAL_PID) as handle:
        exchange_normal(handle, request)


def wait_for_hid(vid: int, pid: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hid = _hid_module()
        if hid.enumerate(vid, pid):
            return
        time.sleep(0.5)
    raise ProtocolError(f"timed out waiting for HID {vid:04x}:{pid:04x}")


def restore_blob(
    blob: bytes,
    plan: UpdatePlan,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Transfer a prepared image to an already-enumerated bootloader HID."""

    with HidHandle(BOOT_VID, BOOT_HID_PID) as handle:
        handle.write_exact(to_boot_hid_report(build_boot_metadata_request(plan)))
        reply = from_boot_hid_report(handle.read(BOOT_REPORT_SIZE, 10000))
        if decode_boot_ack(reply) != 0:
            raise ProtocolError("unexpected metadata acknowledgement")

        for sequence, frame in iter_data_frames(blob):
            handle.write_exact(to_boot_hid_report(frame))
            final = sequence + 1 == plan.packet_count
            timeout = 120000 if final else 10000
            reply = from_boot_hid_report(handle.read(BOOT_REPORT_SIZE, timeout))
            if final:
                if not decode_boot_terminal_status(reply):
                    raise ProtocolError("bootloader rejected the whole-image MD5")
            else:
                expected = sequence + 1
                acknowledged = decode_boot_ack(reply)
                if acknowledged != expected:
                    raise ProtocolError(
                        "bootloader requested packet "
                        f"{acknowledged} after packet {sequence}; retry is not implemented"
                    )
            if progress:
                progress(sequence + 1, plan.packet_count)

"""Command-line interface for cc2flash."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from . import __version__
from .adb_backup import (
    AdbClient,
    AdbUnavailable,
    acquire_twice,
    hashes,
    save_backup,
    validate_preserved_backup,
)
from .hid_transport import (
    ADB_STARTUP_CONTENT,
    ADB_STARTUP_PATH,
    enter_bootloader,
    expected_devices,
    install_adb_startup,
    restore_blob,
    wait_for_hid,
)
from .protocol import (
    BOOT_HID_PID,
    BOOT_VID,
    FLASH_SIZE,
    NORMAL_PID,
    NORMAL_VID,
    ProtocolError,
    build_update_blob,
    validate_full_restore_image,
)


def _adb(args) -> AdbClient:
    return AdbClient(args.adb, args.serial)


def command_list(args) -> int:
    result: dict[str, object] = {"hid": [], "adb": []}
    try:
        for item in expected_devices():
            result["hid"].append(
                {
                    "vid": f"{item.get('vendor_id', 0):04x}",
                    "pid": f"{item.get('product_id', 0):04x}",
                    "product": item.get("product_string"),
                    "serial": item.get("serial_number"),
                }
            )
    except ProtocolError as exc:
        result["hid_error"] = str(exc)
    try:
        output = _adb(args).run("devices", "-l").decode("utf-8", "replace")
        result["adb"] = [line for line in output.splitlines()[1:] if line.strip()]
    except ProtocolError as exc:
        result["adb_error"] = str(exc)
    print(json.dumps(result, indent=2))
    return 0


def command_info(args) -> int:
    identity, parts = _adb(args).identity_and_partitions()
    print(f"ADB identity: {identity}")
    print("Validated flash: 8 MiB ZB25VQ64-compatible layout")
    offset = 0
    for part in parts:
        print(
            f"mtd{part.index}: 0x{offset:06x}-0x{offset + part.size - 1:06x} "
            f"0x{part.size:x} {part.name}"
        )
        offset += part.size
    return 0


def command_backup(args) -> int:
    output = Path(args.output)
    try:
        image, manifest = acquire_twice(_adb(args))
    except AdbUnavailable as exc:
        return _bootstrap_adb_for_backup(args, exc)
    manifest_path = save_backup(output, image, manifest)
    print("Backup complete (two identical full reads)")
    print(f"Size:   {manifest['size']}")
    print(f"SHA256: {manifest['sha256']}")
    print(f"MD5:    {manifest['md5']}")
    print(f"Saved:  {output}")
    print(f"Manifest: {manifest_path}")
    return 0


def _bootstrap_adb_for_backup(args, unavailable: AdbUnavailable) -> int:
    """Offer the one-time persistent startup hook, then require a restart."""

    print(f"ADB is unavailable: {unavailable}", file=sys.stderr)
    print("ADB startup recovery will modify the camera:", file=sys.stderr)
    print(f"  overwrite {ADB_STARTUP_PATH}", file=sys.stderr)
    print(
        f"  with the exact {len(ADB_STARTUP_CONTENT)} bytes: "
        f"{ADB_STARTUP_CONTENT.decode('ascii')}",
        file=sys.stderr,
    )
    print("No flash backup can be read until after a manual restart.", file=sys.stderr)

    if not args.bootstrap_adb:
        if not sys.stdin.isatty():
            raise ProtocolError(
                "ADB startup recovery requires an interactive confirmation or "
                "the explicit --bootstrap-adb option"
            )
        phrase = input("Type ENABLE-ADB to overwrite the startup hook: ")
        if phrase != "ENABLE-ADB":
            raise ProtocolError("confirmation did not match; camera was not modified")

    install_adb_startup()
    print(f"Installed ADB startup hook: {ADB_STARTUP_PATH}")
    print("No flash was read and no backup file was created.")
    print("Restart or power-cycle the camera, wait for normal USB mode, then rerun:")
    rerun = ["cc2flash", "backup"]
    if args.adb != "adb":
        rerun += ["--adb", args.adb]
    if args.serial:
        rerun += ["--serial", args.serial]
    rerun.append(args.output)
    print("  " + " ".join(rerun))
    return 3


def command_plan(args) -> int:
    image = Path(args.image).read_bytes()
    validate_full_restore_image(image)
    blob, plan = build_update_blob(image)
    result = {
        "image": str(Path(args.image)),
        "image_size": len(image),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "image_md5": hashlib.md5(image).hexdigest(),
        "flash_offset": plan.flash_offset,
        "transfer_size": plan.transfer_size,
        "packets": plan.packet_count,
        "packet_payload": plan.packet_payload_size,
        "metadata_and_image_header_built": bool(blob),
        "writes_performed": False,
    }
    print(json.dumps(result, indent=2))
    return 0


def _confirm(image_path: Path, image_hashes: dict, backup_path: Path) -> None:
    print("WRITE OPERATION")
    print(f"Input:  {image_path}")
    print(f"Range:  0x000000-0x{FLASH_SIZE - 1:06x}")
    print(f"SHA256: {image_hashes['sha256']}")
    print(f"Backup: {backup_path}")
    phrase = input("Type RESTORE-CC2 to continue: ")
    if phrase != "RESTORE-CC2":
        raise ProtocolError("confirmation did not match; nothing was written")


def command_restore(args) -> int:
    image_path = Path(args.image)
    backup_path = Path(args.backup)
    backup_hashes = validate_preserved_backup(backup_path)
    image = image_path.read_bytes()
    validate_full_restore_image(image)
    image_hashes = hashes(image)
    if image_hashes["sha256"] == backup_hashes["sha256"]:
        print("Note: replacement image is byte-identical to the preserved backup.")
    blob, plan = build_update_blob(image)
    if not args.yes:
        _confirm(image_path, image_hashes, backup_path)

    print("Entering bootloader HID mode; the 8-byte flag write begins now.")
    enter_bootloader()
    wait_for_hid(BOOT_VID, BOOT_HID_PID, timeout=args.enumeration_timeout)

    last_percent = -1

    def progress(done: int, total: int) -> None:
        nonlocal last_percent
        percent = done * 100 // total
        if percent != last_percent and (percent % 5 == 0 or done == total):
            print(f"Transfer: {percent}% ({done}/{total})")
            last_percent = percent

    restore_blob(blob, plan, progress=progress)
    print("Bootloader accepted the image MD5 and has started erase/write.")
    print("Do not disconnect power; waiting for normal-mode USB to return.")
    wait_for_hid(NORMAL_VID, NORMAL_PID, timeout=args.reboot_timeout)

    if args.no_post_verify:
        print("Normal mode returned. Post-write readback was explicitly skipped.")
        return 0

    print("Normal mode returned; reading flash twice for post-write verification.")
    deadline = time.monotonic() + args.adb_timeout
    while True:
        try:
            verified, _manifest = acquire_twice(_adb(args))
            break
        except ProtocolError:
            if time.monotonic() >= deadline:
                raise ProtocolError(
                    "normal HID returned, but ADB readback did not become available; "
                    "write is not post-verified"
                )
            time.sleep(1)
    if verified != image:
        raise ProtocolError("post-write flash readback differs from the replacement image")
    print("Restore complete: two post-write reads exactly match the input image.")
    print(f"SHA256: {image_hashes['sha256']}")
    return 0


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--adb", default="adb", help="ADB executable (default: adb)")
    common.add_argument("--serial", help="ADB device serial")

    result = argparse.ArgumentParser(prog="cc2flash")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", parents=[common], help="list USB/ADB devices; read-only")
    list_parser.set_defaults(func=command_list)

    info = commands.add_parser("info", parents=[common], help="validate and show MTD layout; read-only")
    info.set_defaults(func=command_info)

    backup = commands.add_parser(
        "backup",
        parents=[common],
        help="read flash twice; offer normal-HID ADB startup recovery if absent",
    )
    backup.add_argument("output")
    backup.add_argument(
        "--bootstrap-adb",
        action="store_true",
        help=(
            "if no ADB device is connected, overwrite /etc/conf.d/system.sh "
            "with '/bin/adbd &' without an interactive prompt"
        ),
    )
    backup.set_defaults(func=command_backup)

    plan = commands.add_parser("plan-restore", help="validate and describe an image; no USB writes")
    plan.add_argument("image")
    plan.set_defaults(func=command_plan)

    restore = commands.add_parser("restore", parents=[common], help="restore one full 8 MiB image")
    restore.add_argument("image")
    restore.add_argument("--backup", required=True, help="preserved backup with cc2flash manifest")
    restore.add_argument("--yes", action="store_true", help="skip typed confirmation")
    restore.add_argument("--no-post-verify", action="store_true", help="skip normal-mode ADB readback")
    restore.add_argument("--enumeration-timeout", type=float, default=30)
    restore.add_argument("--reboot-timeout", type=float, default=180)
    restore.add_argument("--adb-timeout", type=float, default=60)
    restore.set_defaults(func=command_restore)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ProtocolError, OSError) as exc:
        print(f"cc2flash: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

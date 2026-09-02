"""Command-line interface for cc2flash."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from . import __version__
from .adb_backup import (
    AdbClient,
    AdbUnavailable,
    acquire_stable,
    hashes,
    load_preserved_backup,
    save_backup,
    validate_backup_archive_path,
    validate_post_restore_readback,
    validate_replacement_against_backup,
)
from .hid_transport import (
    ADB_STARTUP_CONTENT,
    ADB_STARTUP_PATH,
    enter_bootloader,
    expected_devices,
    install_adb_startup,
    restore_blob,
    start_adb_through_upload_command,
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


def _common_command(args, command: str) -> list[str]:
    result = ["cc2flash", command]
    if args.adb != "adb":
        result += ["--adb", args.adb]
    if args.serial:
        result += ["--serial", args.serial]
    return result


def _read_progress(attempt, maximum, consecutive, required) -> None:
    print(
        f"Flash read {attempt}/{maximum} complete; "
        f"consecutive identical: {consecutive}/{required}"
    )


def _sha256_argument(value: str) -> str:
    normalized = value.casefold()
    if len(normalized) != 64:
        raise argparse.ArgumentTypeError("SHA-256 must contain exactly 64 hex digits")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("SHA-256 must contain only hex digits") from exc
    return normalized


def _positive_finite_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError(
            "duration must be a finite positive number"
        )
    return duration


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
    # Reject a non-atomic destination form before spending time on any physical
    # reads. A single ZIP is the publication unit consumed by restore.
    validate_backup_archive_path(output)
    adb = _adb(args)

    # Safety ordering is intentional and must not be reversed:
    #
    # 1. Read the complete six-partition image until three consecutive 8 MiB
    #    results are identical (with five total attempts at most).
    # 2. Fingerprint the boot partition from that stable image.
    # 3. Require the built-in reference or an exact reviewed override.
    # 4. Publish the image and manifest atomically.
    #
    # In particular, a hash from a lone or changing read is not useful as an
    # acceptance token. acquire_stable() computes and returns the boot hash only
    # after stability; every failure below happens before save_backup().
    try:
        image, manifest = acquire_stable(adb, progress=_read_progress)
    except AdbUnavailable as exc:
        temporary = " ".join(_common_command(args, "start-adb"))
        persistent = " ".join(_common_command(args, "install-adb-startup"))
        raise ProtocolError(
            f"{exc}\n"
            "backup is strictly read-only and did not modify the camera.\n"
            "Start ADB with one of these explicit commands, then rerun backup:\n"
            f"  temporary for this boot: {temporary}\n"
            f"  persistent after restart: {persistent}"
        ) from exc

    bootloader = manifest["bootloader"]
    observed = str(bootloader["sha256"])
    accepted = args.accept_bootloader_hash
    if accepted is not None and accepted != observed:
        raise ProtocolError(
            "--accept-bootloader-hash does not match the observed boot partition: "
            f"observed {observed}"
        )
    if bootloader["known_reference"]:
        bootloader["acceptance"] = "known-reference"
    elif accepted == observed:
        bootloader["acceptance"] = "explicit-hash"
    else:
        rerun = _common_command(args, "backup")
        rerun += ["--accept-bootloader-hash", observed, str(output)]
        raise ProtocolError(
            "unknown bootloader SHA-256; no backup was published:\n"
            f"  {observed}\n"
            "After independently reviewing that exact hash, accept it with:\n"
            f"  {' '.join(rerun)}"
        )

    archive_path = save_backup(output, image, manifest)
    print(
        "Backup complete (three consecutive identical full reads; "
        f"{manifest['read_passes']} total attempt(s))"
    )
    print(f"Size:   {manifest['size']}")
    print(f"SHA256: {manifest['sha256']}")
    print(f"MD5:    {manifest['md5']}")
    print(f"Boot:   {observed}")
    print(
        "Known bootloader: "
        f"{'yes' if bootloader['known_reference'] else 'no (explicitly accepted)'}"
    )
    print(f"Saved archive: {archive_path}")
    print("Archive members: flash.bin, manifest.json")
    return 0


def command_start_adb(args) -> int:
    """Start adbd for this boot through the immutable HID injection target."""

    adb = _adb(args)
    try:
        adb.ensure_available()
    except AdbUnavailable as unavailable:
        print(f"ADB is unavailable: {unavailable}")
    else:
        identity, _parts = adb.identity_and_partitions()
        print(f"ADB is already online; no HID command was sent. Identity: {identity}")
        print("You can now run cc2flash backup <output.zip>.")
        return 0

    print(
        "Starting /bin/adbd once through the normal-HID upload command. "
        "No persistent startup file will be installed."
    )
    print(
        "The final upload commit is expected to report failure or disconnect; "
        "ADB availability is the success signal."
    )
    start_adb_through_upload_command()
    adb.wait_for_device(timeout=args.timeout)
    identity, _parts = adb.identity_and_partitions()
    print(f"ADB started for this boot. Root identity: {identity}")
    print("No flash backup was read and no persistent startup file was installed.")
    print("Now run cc2flash backup <output.zip>.")
    return 0


def command_install_adb_startup(args) -> int:
    """Install the persistent startup hook as a separate explicit operation."""

    adb = _adb(args)
    try:
        adb.ensure_available()
    except AdbUnavailable as unavailable:
        print(f"ADB is unavailable: {unavailable}", file=sys.stderr)
    else:
        print("ADB is already online; no persistent startup file was installed.")
        print("You can now run cc2flash backup <output.zip>.")
        return 0

    print("Persistent ADB installation will modify the camera:", file=sys.stderr)
    print(f"  overwrite {ADB_STARTUP_PATH}", file=sys.stderr)
    print(
        f"  with the exact {len(ADB_STARTUP_CONTENT)} bytes: "
        f"{ADB_STARTUP_CONTENT.decode('ascii')}",
        file=sys.stderr,
    )
    print("No flash backup can be read until after a manual restart.", file=sys.stderr)

    if not args.yes:
        if not sys.stdin.isatty():
            raise ProtocolError(
                "persistent ADB installation requires an interactive confirmation "
                "or the explicit --yes option"
            )
        phrase = input("Type ENABLE-ADB to overwrite the startup hook: ")
        if phrase != "ENABLE-ADB":
            raise ProtocolError("confirmation did not match; camera was not modified")

    install_adb_startup()
    print(f"Installed ADB startup hook: {ADB_STARTUP_PATH}")
    print("No flash was read and no backup file was created.")
    print("Restart or power-cycle the camera, wait for normal USB mode, then run:")
    print("  " + " ".join(_common_command(args, "backup")) + " <output.zip>")
    return 3


def command_plan(args) -> int:
    image_path = Path(args.image)
    image = image_path.read_bytes()
    validate_full_restore_image(image)
    backup_path = Path(args.backup) if args.backup else None
    backup_hashes = None
    if backup_path is not None:
        backup_image, backup_hashes = load_preserved_backup(backup_path)
        validate_replacement_against_backup(image, backup_image)
    blob, plan = build_update_blob(image)
    result = {
        "image": str(image_path),
        "image_size": len(image),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "image_md5": hashlib.md5(image).hexdigest(),
        "preserved_backup": str(backup_path) if backup_path else None,
        "preserved_backup_sha256": (
            backup_hashes["sha256"] if backup_hashes else None
        ),
        "preserved_backup_compatible": backup_path is not None,
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


def _confirm_temporary_adb_for_readback() -> None:
    """Require separate consent before starting adbd after a restore."""

    if not sys.stdin.isatty():
        raise ProtocolError(
            "ADB is offline after restore; temporary ADB startup requires an "
            "interactive confirmation. No HID ADB-start command was sent, and "
            "the restore is not post-verified"
        )
    answer = input(
        "ADB is offline after restore. Start /bin/adbd temporarily through "
        "normal HID for post-write readback? [y/N] "
    )
    if answer.strip().casefold() not in {"y", "yes"}:
        raise ProtocolError(
            "temporary ADB startup was declined. No HID ADB-start command was "
            "sent, and the restore is not post-verified"
        )


def command_restore(args) -> int:
    image_path = Path(args.image)
    backup_path = Path(args.backup)
    backup_image, backup_hashes = load_preserved_backup(backup_path)
    image = image_path.read_bytes()
    validate_full_restore_image(image)
    validate_replacement_against_backup(image, backup_image)
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

    print(
        "Normal mode returned; requiring three consecutive identical flash "
        "reads for post-write verification."
    )
    adb = _adb(args)
    try:
        adb.ensure_available()
    except AdbUnavailable:
        _confirm_temporary_adb_for_readback()
        try:
            start_adb_through_upload_command()
        except ProtocolError as exc:
            raise ProtocolError(
                "normal HID returned, but the explicitly confirmed temporary "
                "ADB start failed; write is not post-verified"
            ) from exc
    try:
        # --adb-timeout deliberately bounds only the transition to an online
        # daemon. Stable acquisition is a separate operation: each pull keeps
        # its normal command timeout and no completed read is discarded merely
        # because the availability window expired while verification ran.
        adb.wait_for_device(timeout=args.adb_timeout)
    except ProtocolError as exc:
        raise ProtocolError(
            "normal HID returned, but ADB readback did not become available; "
            "write is not post-verified"
        ) from exc
    try:
        verified, _manifest = acquire_stable(adb, progress=_read_progress)
    except ProtocolError as exc:
        raise ProtocolError(
            "ADB became available, but stable post-write readback failed; "
            f"write is not post-verified: {exc}"
        ) from exc
    config_exact = validate_post_restore_readback(image, verified)
    if config_exact:
        print(
            "Restore complete: three consecutive post-write reads exactly "
            "match the input image."
        )
    else:
        print(
            "Restore complete: three consecutive post-write reads match every "
            "boot-stable byte through HWCONFIG. Config changed during the "
            "required verification boot and was not claimed byte-exact."
        )
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
        help=(
            "require three consecutive identical flash reads within five attempts; "
            "strictly read-only"
        ),
    )
    backup.add_argument(
        "output",
        help="single ZIP archive to publish; must end in .zip",
    )
    backup.add_argument(
        "--accept-bootloader-hash",
        type=_sha256_argument,
        help=(
            "accept one reviewed unknown boot-partition SHA-256; the supplied "
            "value must exactly match the observed hash"
        ),
    )
    backup.set_defaults(func=command_backup)

    start_adb = commands.add_parser(
        "start-adb",
        parents=[common],
        help=(
            "start root ADB temporarily through normal HID; no persistent file"
        ),
    )
    start_adb.add_argument(
        "--timeout",
        type=_positive_finite_duration,
        default=30,
        help="seconds to wait for ADB after temporary HID startup (default: 30)",
    )
    start_adb.set_defaults(func=command_start_adb)

    install_adb = commands.add_parser(
        "install-adb-startup",
        parents=[common],
        help="persistently install /etc/conf.d/system.sh; restart required",
    )
    install_adb.add_argument(
        "--yes",
        action="store_true",
        help="skip typed ENABLE-ADB confirmation",
    )
    install_adb.set_defaults(func=command_install_adb_startup)

    plan = commands.add_parser("plan-restore", help="validate and describe an image; no USB writes")
    plan.add_argument("image")
    plan.add_argument(
        "--backup",
        help=(
            "preserved cc2flash ZIP; also prove the image differs only in "
            "hardware-recovery's audited regions"
        ),
    )
    plan.set_defaults(func=command_plan)

    restore = commands.add_parser("restore", parents=[common], help="restore one full 8 MiB image")
    restore.add_argument("image")
    restore.add_argument(
        "--backup",
        required=True,
        help="preserved cc2flash .zip archive",
    )
    restore.add_argument("--yes", action="store_true", help="skip typed confirmation")
    restore.add_argument("--no-post-verify", action="store_true", help="skip normal-mode ADB readback")
    restore.add_argument(
        "--enumeration-timeout", type=_positive_finite_duration, default=30
    )
    restore.add_argument(
        "--reboot-timeout", type=_positive_finite_duration, default=180
    )
    restore.add_argument(
        "--adb-timeout",
        type=_positive_finite_duration,
        default=60,
        help=(
            "seconds to wait for ADB to become online before post-write "
            "verification starts (default: 60; does not cap the reads)"
        ),
    )
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

"""Command-line interface for cc2flash."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from . import __version__
from . import image as image_tools
from .display import invocation
from .startup import install_startup
from .restore_prepare import prepare_restore, validate_preparation_image
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
    enter_bootloader,
    expected_devices,
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
            f"  persistent after restart (requires an existing backup): {persistent} --backup <backup.zip>"
        ) from exc

    bootloader = manifest["bootloader"]
    observed = str(bootloader["sha256"])
    accepted = args.accept_bootloader_sha256
    if accepted is not None and accepted != observed:
        raise ProtocolError(
            "--accept-bootloader-sha256 does not match the observed boot partition: "
            f"observed {observed}"
        )
    if bootloader["known_reference"]:
        bootloader["acceptance"] = "known-reference"
    elif accepted == observed:
        bootloader["acceptance"] = "explicit-hash"
    else:
        rerun = _common_command(args, "backup")
        rerun += ["--accept-bootloader-sha256", observed, str(output)]
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
    print("Next, build this camera's recovery image:")
    print(invocation("build-image", str(archive_path)))
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
    print("Next:")
    print(invocation(*_common_command(args, "backup")[1:], "backup.zip"))
    return 0


def command_install_startup(args) -> int:
    """Install a selected boot hook after explicit persistent-write consent."""
    feature = args.functionality
    phrase = "ENABLE-ADB" if feature == "adb" else "INSTALL-ERASE-FIX"
    print(f"Install {feature} in /etc/conf.d/enabled and the shared system.sh runner.", file=sys.stderr)
    print("This writes config files. A same-camera backup and safe free space are required.", file=sys.stderr)
    print("The erase fix is experimental and hardware-unverified; installation does not remount config.", file=sys.stderr)
    if not args.yes:
        if not sys.stdin.isatty():
            raise ProtocolError("hook installation requires interactive confirmation or explicit --yes")
        if input(f"Type {phrase} to install: ") != phrase:
            raise ProtocolError("confirmation did not match; camera was not modified")
    result = install_startup(_adb(args), Path(args.backup), functionality=feature, progress=_read_progress)
    if result is None:
        print("Exact hook and runner already present; no persistent write needed.")
    else:
        print(f"Installed and read back hook files; reserved write budget: {result.write_budget} bytes.")
    print("Restart manually to verify the boot hook; inspect /tmp/cc2-hooks.log afterward.")
    print("No firmware image was flashed. See docs/STARTUP-HOOKS.md for verification and stop conditions.")
    return 0


def command_plan(args) -> int:
    image_path = Path(args.image)
    image = image_path.read_bytes()
    validate_full_restore_image(image)
    backup_path = Path(args.backup) if args.backup else None
    backup_hashes = None
    if backup_path is not None:
        backup_image, backup_hashes = load_preserved_backup(backup_path)
        validate_replacement_against_backup(image, backup_image)
        validate_preparation_image(backup_image)
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
    if not sys.stdin.isatty():
        raise ProtocolError("restore requires an interactive RESTORE-CC2 confirmation; nothing was written")
    print("WRITE OPERATION")
    print(f"Input:  {image_path}")
    print(f"Range:  0x000000-0x{FLASH_SIZE - 1:06x}")
    print(f"SHA256: {image_hashes['sha256']}")
    print(f"Backup: {backup_path}")
    print("Requires online root ADB; temporarily sets the driver erase size to 4 KiB.")
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
    if args.dry_run:
        return command_plan(args)
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
    validate_preparation_image(backup_image)
    _confirm(image_path, image_hashes, backup_path)

    print("Validating the live camera and preparing its temporary SFC erase size.")
    prepare_restore(_adb(args), backup_image, progress=_read_progress)
    print("Entering bootloader HID mode; the 8-byte flag write begins now.")
    enter_bootloader()
    wait_for_hid(BOOT_VID, BOOT_HID_PID, timeout=args.bootloader_timeout)

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

    print(
        "Normal mode returned; requiring three consecutive identical flash "
        "reads for post-write verification."
    )
    adb = _adb(args)
    adb_deadline = time.monotonic() + args.adb_timeout
    try:
        remaining = adb_deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(
                "post-restore ADB availability timeout expired; write is not "
                "post-verified"
            )
        adb.ensure_available(timeout=min(10.0, remaining))
    except AdbUnavailable:
        _confirm_temporary_adb_for_readback()
        remaining = adb_deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(
                "post-restore ADB availability timeout expired before the "
                "temporary start. No HID ADB-start command was sent, and the "
                "restore is not post-verified"
            )
        try:
            start_adb_through_upload_command()
        except ProtocolError as exc:
            raise ProtocolError(
                "normal HID returned, but the explicitly confirmed temporary "
                "ADB start failed; write is not post-verified"
            ) from exc
        remaining = adb_deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(
                "post-restore ADB availability timeout expired after the "
                "confirmed temporary start; write is not post-verified"
            )
        try:
            adb.wait_for_device(timeout=remaining)
        except ProtocolError as exc:
            raise ProtocolError(
                "normal HID returned, but ADB readback did not become available; "
                "write is not post-verified"
            ) from exc
        # --adb-timeout deliberately bounds only the transition to an online
        # daemon. Stable acquisition is a separate operation: each pull keeps
        # its normal command timeout and no completed read is discarded merely
        # because the availability window expired while verification ran.
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
    common.add_argument("--serial", help="ADB device serial; not the camera's embedded identity")

    result = argparse.ArgumentParser(prog="cc2flash", description="Back up, inspect, repair and restore the supported Elegoo CC2 stock camera.")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)

    devices = commands.add_parser("devices", help="list USB/ADB devices; read-only")
    devices.add_argument("--adb", default="adb", help="ADB executable (default: adb)")
    devices.set_defaults(func=command_list, serial=None)
    info = commands.add_parser("device-info", parents=[common], help="validate root ADB and show flash layout; read-only")
    info.set_defaults(func=command_info)

    backup = commands.add_parser("backup", parents=[common], help="save three consecutive identical flash reads; read-only")
    backup.add_argument("output", help="new backup archive ending in .zip")
    backup.add_argument("--accept-bootloader-sha256", type=_sha256_argument,
                        help="accept exactly one independently reviewed unknown boot-partition SHA-256")
    backup.set_defaults(func=command_backup)

    start = commands.add_parser("start-adb", parents=[common], help="start root ADB for this boot; no persistent file")
    start.add_argument("--timeout", type=_positive_finite_duration, default=30, help="ADB startup wait in seconds (default: 30)")
    start.set_defaults(func=command_start_adb)
    for name, feature, description in (
        ("install-adb-startup", "adb", "install a persistent ADB hook; requires online ADB and safe config space"),
        ("install-erase-fix", "erase-fix", "install experimental early-boot JFFS2 erase correction; no firmware flashing"),
    ):
        install = commands.add_parser(name, parents=[common], help=description)
        install.add_argument("--backup", required=True, help="preserved same-camera backup ZIP")
        install.add_argument("--yes", action="store_true", help="explicit consent to persistent config writes")
        install.set_defaults(func=command_install_startup, functionality=feature)

    inspect = commands.add_parser("inspect-image", help="validate a raw dump or backup ZIP; offline, no output files")
    build = commands.add_parser("build-image", help="build a camera-specific recovery bundle; offline")
    for command in (inspect, build):
        command.add_argument("image", metavar="INPUT", help="raw 8 MiB dump or unmodified cc2flash backup ZIP")
        command.add_argument("--confirm-read", action="append", default=[], metavar="DUMP",
                             help="additional independent read that must match; repeat for each file")
        command.add_argument("--show-identifiers", action="store_true", help="show full unit identifiers in the analysis report")
    inspect.set_defaults(func=image_tools.cmd_analyze)
    build.add_argument("-o", "--output", metavar="DIR", help="new output directory (default: <input-stem>-cc2-recovery)")
    build.add_argument("--config-mode", choices=("serial-only", "preserve-files"), default="serial-only",
                       help="rebuild only serial.cfg or all supported live files (default: serial-only)")
    build.add_argument("--wipe-unknown-config", action="store_true", help="with serial-only, explicitly discard unfamiliar config names")
    build.add_argument("--allow-fewer-reads", action="store_true", help="explicitly accept fewer than three matching reads; other validation remains mandatory")
    build.set_defaults(func=image_tools.cmd_build)

    restore = commands.add_parser("restore", parents=[common], help="write and verify a full camera image, or check it offline with --dry-run")
    restore.add_argument("image", metavar="IMAGE")
    restore.add_argument("--backup", required=True, metavar="BACKUP.zip", help="preserved three-read cc2flash backup archive")
    restore.add_argument("--dry-run", action="store_true", help="local validation only; no USB or ADB access")
    restore.add_argument("--bootloader-timeout", type=_positive_finite_duration, default=30, help="bootloader USB wait in seconds (default: 30)")
    restore.add_argument("--reboot-timeout", type=_positive_finite_duration, default=180, help="normal USB return wait in seconds (default: 180)")
    restore.add_argument("--adb-timeout", type=_positive_finite_duration, default=60, help="post-write ADB availability wait, not the read duration (default: 60)")
    restore.set_defaults(func=command_restore)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command_parser = parser()
    args = command_parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ProtocolError, image_tools.ValidationError, OSError, EOFError) as exc:
        print(f"cc2flash: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

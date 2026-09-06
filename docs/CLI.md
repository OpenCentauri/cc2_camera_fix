# cc2flash command reference

Install using the [setup guide](INSTALLATION.md). Start with the workflows in
the [main guide](../README.md); these options are for reference.
Every command accepts `--help`. `cc2flash --version` reports the installed version.
On Windows PowerShell, use `.\cc2flash.exe` for a downloaded executable in the
current folder. Python installations also support `python -m cc2flash`.

## Connected camera

```text
cc2flash devices [--adb EXECUTABLE]
cc2flash device-info [--adb EXECUTABLE] [--serial SERIAL]
cc2flash start-adb [--adb EXECUTABLE] [--serial SERIAL] [--timeout SECONDS]
cc2flash install-adb-startup [--adb EXECUTABLE] [--serial SERIAL] [--yes]
cc2flash backup OUTPUT.zip [--adb EXECUTABLE] [--serial SERIAL]
    [--accept-bootloader-sha256 SHA256]
```

- `devices`: best-effort, read-only USB/HID and ADB listing.
- `device-info`: validate root ADB and the supported MTD partition layout.
  This is not a complete firmware-image inspection.
- `start-adb`: temporarily start the existing daemon; no persistent startup
  file. Default timeout: 30 seconds.
- `install-adb-startup`: overwrite `/etc/conf.d/system.sh` with the known
  ADB startup content. Requires `ENABLE-ADB` confirmation or `--yes`.
  Restart manually afterward; successful installation exits with status 0.
  If ADB is already online, this command does not install anything.
- `backup`: require three consecutive identical full reads within five
  attempts, then publish a new ZIP atomically. Never overwrites a backup.
  If ADB is offline, explicitly start it and rerun backup; backup itself is
  read-only. A reviewed unknown bootloader can be accepted by its exact
  SHA-256 without bypassing any other check.

`--adb` defaults to `adb`; a path to the executable is also accepted.
`--serial` is the USB ADB serial, not the identifier embedded in the firmware.
It does not relax the restore requirement for exactly one connected camera.

## Offline image work

```text
cc2flash inspect-image INPUT [--confirm-read DUMP]... [--show-identifiers]
cc2flash build-image INPUT [--confirm-read DUMP]... [-o DIR | --output DIR]
    [--config-mode {serial-only,preserve-files}]
    [--wipe-unknown-config] [--allow-fewer-reads] [--show-identifiers]
```

`INPUT` is a raw 8 MiB dump or an unmodified cc2flash backup ZIP.
Inspection validates without creating files; building creates a recovery bundle
in a new `<input-stem>-cc2-recovery/` directory unless `--output` is given.
Existing output directories are refused. Neither command accesses hardware.

For raw dumps, repeat `--confirm-read` for each independent matching read:

```sh
cc2flash build-image camera-1.bin --confirm-read camera-2.bin --confirm-read camera-3.bin
```

A valid ZIP supplies its own three-consecutive-read evidence. Non-reference raw
inputs require three matching reads unless `--allow-fewer-reads` explicitly
accepts the higher risk. This exception is recorded in the bundle and does not
bypass firmware, identity, or mismatch checks. Exact full reference hashes can
be rebuilt from one copy.

| Config mode | Behavior |
|---|---|
| `serial-only` (default) | Rebuild config with only the camera's original serial.cfg; missing defaults are created at next boot. |
| `preserve-files` | Rebuild every supported live regular file once, preserving contents and metadata while dropping obsolete records. |

`--wipe-unknown-config` permits discarding unfamiliar names only with
`serial-only`. It never permits discarding the camera's identity.
`--show-identifiers` reveals identifiers in analysis reports; default reports
mask them. Raw dumps, ZIPs, serial.cfg, and recovery images always contain
private unit data regardless of report masking.

## USB restore

```text
cc2flash restore IMAGE --backup BACKUP.zip --dry-run
cc2flash restore IMAGE --backup BACKUP.zip
    [--adb EXECUTABLE] [--serial SERIAL]
    [--bootloader-timeout SECONDS] [--reboot-timeout SECONDS]
    [--adb-timeout SECONDS]
```

`--dry-run` validates the candidate, preserved backup, allowed changes, known
preparation kernel, and transfer representation entirely offline. It opens no
transport and writes nothing. Hardware-specific options are rejected with it.
It cannot certify the live camera or predict successful physical writing.

A real restore repeats local validation, requires typed `RESTORE-CC2` consent,
validates the live camera, applies the temporary SFC correction, and writes the
full image. Keep power connected until post-write verification completes.
If needed after reboot, temporary ADB startup requires separate consent.
There is no noninteractive write bypass or post-verification opt-out.

Timeouts default to 30 seconds for bootloader USB, 180 for normal USB return,
and 60 for post-write ADB availability. The last timeout does not cap the flash
reads themselves.

Verification requires three consecutive identical reads and exact agreement
through the end of HWCONFIG. Config may change during the required boot; the
output states explicitly whether config also matched byte-for-byte.

## External programmer verification

Require full-chip verification against the generated 8 MiB image in the
programmer software. If saving an independent readback, compare it with
`fc.exe /b expected.bin readback.bin` on Windows or
`cmp expected.bin readback.bin` on Linux/macOS. Any mismatch is a failure.
The tool does not operate an external programmer.

## Exit status

0 means the requested operation succeeded. 2 means a CLI, validation, or
operating-system error. A successful dry run is not a successful physical
restore. A failed post-write check means a write may already have occurred;
read the error and keep the original backup.

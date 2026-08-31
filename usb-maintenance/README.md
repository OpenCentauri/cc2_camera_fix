# cc2flash

`cc2flash` is a safety-focused maintenance client and protocol description for
the stock Elegoo Centauri Carbon 2 camera firmware found in the supplied
8 MiB flash dump.

> **Research status:** the library now decodes bootloader type-2 ACK payloads as
> `u32le(next_expected_packet)` and rejects unexpected/retry requests instead of
> treating them as zero. Destructive restore is still hardware-unverified, and
> automatic retransmission is not implemented. Preserve a two-pass backup and
> read `PROTOCOL.md`, sections 9, 10, and 12 before considering a write.

The important result is that the bootloader USB updater is **write-only**. It
does not implement a flash-read request. The included backup implementation
instead reads `/dev/mtd0` through `/dev/mtd5` twice over root ADB. If ADB is
absent, `backup --start-adb-through-upload-command` can start `/bin/adbd` for the
current boot through the normal-HID uploader's unquoted shell command and then
continue the read without restarting. The older `--bootstrap-adb` alternative
overwrites `/etc/conf.d/system.sh` with `/bin/adbd &`; the root startup script
executes that persistent hook on the next boot.

This code was recovered by static analysis and tested offline against the
provided dump. The startup hook itself was runtime-validated manually by the
device owner; this client-generated HID sequence has not yet been captured on
physical hardware. Capture a two-pass backup before considering restore.

## Human-readable source reconstruction

The `source-reconstruction/` directory translates the update-relevant binary
paths into annotated C-like source:

- `hid_update_reconstructed.c` covers the complete normal-mode command parser,
  all configuration and upload commands, the persistent flag write, and the HID
  event loop.
- `spl_upgrade_gate_reconstructed.c` covers the early upgrade-magic decision.
- `uboot_updater_reconstructed.c` covers metadata/data frames, ACK/retry state,
  MD5 validation, transport selection, and SPI erase/write ordering.
- `TRACEABILITY.md` maps functions to binary addresses, and `REPRODUCING.md`
  gives commands to regenerate the relevant disassembly.

This is an audit aid, not recovered original vendor source or a replacement
updater. Read `source-reconstruction/README.md` before relying on it.

## Install

Python 3.10 or newer is required. Backup also requires the Android platform
`adb` executable. Normal-HID ADB recovery and restore support use the optional
`hidapi` package.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[usb]'
```

For inspection and backup when ADB is already running, the package itself has
no Python dependencies:

```sh
python -m pip install -e .
```

## Public Python command API

`cc2flash.commands` mirrors the complete recovered command surface, including
commands that the CLI does not currently use. The module is intentionally
side-effect free and contains large audit comments beside the numeric tables,
wire builders, binary-address anchors, state requirements, and hazards.

The public catalog contains:

- `NormalCommand`: all 57 exact normal-mode values—one version query, 42
  configuration GET/SET commands, 13 file-uploader commands, and canonical
  `0x4000`;
- `CONFIGURATION_COMMANDS`: all 21 literal configuration-key pairs, including
  whether each key was present in the supplied active configuration;
- `UPLOAD_COMMANDS`: the full 13-command uploader state machine;
- `NORMAL_DISPATCH_MAP`: the silent/no-op/unknown behavior for the complete
  16-bit command space, plus group-wide `0x4xxx` matching;
- `BOOT_FRAME_TYPES`: U-Boot types 1, 2, 3, and 5, with directions and payload
  meanings; and
- typed builders and response decoders for every implemented operation.

For example, these calls only construct and inspect bytes; they do not open USB:

```python
from cc2flash.commands import (
    ConfigurationKey,
    NormalCommand,
    build_config_get_request,
    build_config_set_request,
    build_upload_target_request,
    describe_normal_command,
)
from cc2flash.protocol import parse_normal_report

request = build_config_get_request(ConfigurationKey.SENSOR_NAME)
assert parse_normal_report(request).command == 0x2F50

replacement = build_config_set_request("adb_en", "1")
assert parse_normal_report(replacement).command == 0x2F41

# Every recovered literal-path selector is available, not just CLI-used 0x3110.
target = build_upload_target_request(NormalCommand.UPLOAD_TARGET_31F0, "/tmp/a")

# Noncanonical values are reported accurately because all 0x4xxx values reboot.
assert describe_normal_command(0x4ABC).name == "upgrade_and_reboot_0x4abc"
```

Sending is separate and explicit: open `HidHandle` and pass a built report to
`hid_transport.exchange_normal`. Configuration SET, upload commit, and upgrade
reports mutate the device; building them does not.

## Backup workflow

Connect exactly one camera in normal USB mode, then:

```sh
cc2flash list
cc2flash info
cc2flash backup backup.bin
```

Without any setup and before `adbd` starts, the stock camera always exposes its
USB ADB function and appears in `adb devices` as `Ucamera001 offline`. The client
treats that exact offline state like an absent transport for both startup
mechanisms; it does not treat ambiguity, authorization errors, or other ADB
failures the same way.

If ADB is already online, `backup` proceeds without a normal-HID write. To start
the installed daemon only for this boot and continue directly into the read,
opt in explicitly:

```sh
cc2flash backup backup.bin --start-adb-through-upload-command
```

This sends `0x3000`, the immutable no-space target
`/tmp/.cc2flash-adbd-bootstrap;/bin/adbd&` through `0x3110`, one harmless final
newline through `0x3200`, and then `0x3300`. The vulnerable daemon executes:

```sh
rm /tmp/.cc2flash-adbd-bootstrap;/bin/adbd&
```

before attempting to open the complete target as a literal pathname. That open
is expected to fail because the semicolon-suffixed tmpfs component is not a
directory. A status-1 reply or HID disconnect after `0x3300` is therefore not
used as the success signal. Live Windows testing showed that the old USB
transport can first return `error: closed` even though the newly started daemon
already accepts `adb shell`. The client polls through that exact transition,
stock `offline`, and temporary absence for up to 30 seconds, then begins the
normal two-pass acquisition. Other ADB errors and earlier upload failures remain
fatal. Override the wait only when necessary:

```sh
cc2flash backup backup.bin --start-adb-through-upload-command \
  --adb-startup-timeout 60
```

This route does not install a persistent file or require a restart. The vendor
handler nevertheless calls `sync`, and the running stock firmware may already
have pending JFFS2 changes, so it is not a guarantee that unrelated live-system
writes never reach flash. It specifically avoids the known `system.sh` change.

If the temporary route is not selected and no ADB device is connected, an
interactive terminal prints the persistent mutation and requires the literal
confirmation `ENABLE-ADB`. It then sends:

1. `0x3000` — initialize the upload state;
2. `0x3110` — select `/etc/conf.d/system.sh`;
3. `0x3200`, final frame — upload the exact 11 bytes `/bin/adbd &`; and
4. `0x3300` — remove any old target, write the file, and chmod it `0777`.

The command then exits with status `3`. It has not read flash or created
`backup.bin` at that point. Restart or power-cycle the camera and rerun the same
command; `rcS` will start `adbd`, after which the normal two-pass backup runs.

For a noninteractive invocation, the mutation must be requested explicitly:

```sh
cc2flash backup backup.bin --bootstrap-adb
```

This overwrites any existing `/etc/conf.d/system.sh`; the HID protocol cannot
download or preserve the previous file. A missing local `adb` executable,
multiple/unauthorized ADB devices, a non-root shell, or a wrong MTD map does not
trigger the fallback.

`--bootstrap-adb` and `--start-adb-through-upload-command` are mutually
exclusive. Neither is used when the selected ADB device is already available.

`backup` requires the exact recovered partition sequence and sizes:

| MTD | Start | Size | Name |
|---|---:|---:|---|
| mtd0 | `0x000000` | `0x040000` | boot |
| mtd1 | `0x040000` | `0x150000` | kernel |
| mtd2 | `0x190000` | `0x158000` | root |
| mtd3 | `0x2e8000` | `0x4e8000` | system |
| mtd4 | `0x7d0000` | `0x010000` | hwconfig |
| mtd5 | `0x7e0000` | `0x020000` | config |

It writes `backup.bin` only after the two reads compare equal, then creates
`backup.bin.json` with the layout, size, SHA-256, MD5, and acquisition details.
It refuses to overwrite an existing backup.

If ADB lists more than one device, pass the camera serial explicitly:

```sh
cc2flash backup --serial Ucamera001 backup.bin
```

The exact serial exposed by ADB may differ; use `cc2flash list` to inspect it.

## Intended restore workflow (not hardware-ready)

Validate a candidate without opening USB:

```sh
cc2flash plan-restore fixed.bin
```

A full restore requires exactly `0x800000` bytes.  It also rejects an image
whose word at `0x7f8000` is the persistent upgrade magic, because that image
would return to upgrade mode on every reboot.

Restore requires the separately preserved, two-pass backup and its manifest.
The transport parses each nonfinal ACK as the next expected absolute packet and
aborts if U-Boot requests retransmission, which this research client does not yet
implement:

```sh
cc2flash restore fixed.bin --backup backup.bin
```

The command is intended to print the complete target range and hashes, then
require the literal confirmation `RESTORE-CC2`. Its planned phases are:

1. Validate `fixed.bin`, `backup.bin`, and `backup.bin.json` locally.
2. Send the 8-byte upgrade flag through normal Linux HID.
3. Wait for bootloader HID `a108:ff08`.
4. Transfer a 128-byte update header plus the image in numbered packets.
5. Require the bootloader's whole-image MD5 success indication.
6. Wait without removing power while the bootloader erases, writes, and reboots.
7. If the persistent ADB startup hook is present, acquire the flash twice over
   ADB and compare it with `fixed.bin` byte for byte.

The bootloader reports MD5 acceptance **before** erase/write and offers no
post-write status or readback. An independently available ADB or programmer
read is therefore required for end-to-end verification. `--no-post-verify`
gives up that assurance.

Do not unplug the camera after the MD5 response.  A failed transfer before the
MD5 check does not erase flash, but the persistent flag may leave the camera in
bootloader mode; reconnecting the bootloader and retransmitting a full known
image is then the intended recovery path.

## Current limits

- No physical-camera test transcript is included yet.
- Bootloader data ACK parsing now matches the disassembly for in-order packets,
  but automatic retransmission and physical-camera validation remain absent.
- The default/persistent ADB recovery overwrites a startup hook before the first
  backup; the explicit upload-command route avoids that file but deliberately
  relies on a vendor command-injection bug and remains hardware-unverified.
- CDC transport is identified but not implemented; HID is the bootloader's
  default and the path used here.
- Range writes are intentionally omitted.  Although the header accepts an
  arbitrary offset and length, a partial write normally leaves the upgrade
  flag at `0x7f8000` set and can cause an upgrade-mode boot loop.
- The stock bootloader does not bounds-check the image range and does not check
  or report the return values of its SPI erase/write calls.  The client applies
  the missing range checks itself.

See [PROTOCOL.md](PROTOCOL.md) for the recovered wire formats and
[EVIDENCE.md](EVIDENCE.md) for static-analysis offsets and hashes. Read
[PROJECT-STATUS.md](PROJECT-STATUS.md) before running the client.

## Tests

```sh
python -m unittest discover -s tests -v
```

The 50 tests exercise the 57-entry normal command catalog, all configuration and
upload mappings, command builders, U-Boot frame types/ACK decoders, frame
vectors, checksums, image headers, packet numbering, partition validation, ADB
absence/offline/error classification, backup-manifest enforcement, both ADB startup mechanisms,
expected final-commit failure/disconnection, the Windows `error: closed`
transport handoff, CLI mutation guards, and the HID
state machines without opening a device.

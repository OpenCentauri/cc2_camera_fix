# USB-maintenance reference

The normal user journey begins in the [main camera guide](../README.md). This document retains the complete installation, command, API, protocol-status, limitation, and test reference.

`cc2flash` is a safety-focused maintenance client and protocol description for
the stock Elegoo Centauri Carbon 2 camera firmware found in the supplied
8 MiB flash dump.

> **Research status:** a physical test validated in-order bootloader HID
> transfer, full-flash erase/write, normal reboot, video, and independent
> three-read verification on one camera. The same test found that the preceding
> normal-mode trigger can fail destructively when its config-sector target is
> occupied. The integrated client now validates and temporarily corrects that
> live erase-size field; its complete restore path passed physical write and
> three-read verification on the supported camera. Preserve a three-read backup
> and read [SFC restore preparation](SFC-RESTORE-PREPARATION.md) together with
> `PHYSICAL-VALIDATION.md` before restoring.

The important result is that the bootloader USB updater is **write-only**. It
does not implement a flash-read request. The included backup implementation
instead reads `/dev/mtd0` through `/dev/mtd5` over root ADB. It uses the
legacy shell service for text checks and binary-safe `adb pull` for MTD bytes;
the stock daemon does not support modern `adb exec-out`. `backup` is strictly
read-only: if ADB is offline, run the separate `start-adb` command for a
temporary start. With ADB online and a backup preserved, use
`install-adb-startup --backup BACKUP.zip` to install the persistent
`/etc/conf.d/system.sh` hook and then restart.

This code was recovered by static analysis and tested offline against the
provided dump. Live Windows testing has validated temporary HID startup, root
`adb shell`, byte-exact `adb pull`, and a complete 8 MiB backup. Capture a
backup accepted from three consecutive identical reads before considering
restore.

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

Follow the [installation guide](../docs/INSTALLATION.md).
The Windows executable includes Python and HID support; ADB is installed
separately. The complete command syntax is in the [CLI reference](../docs/CLI.md).

For contributor installation, run `python -m pip install -e .` at the repository
root. Run the offline suite there with `python -m unittest discover -s tests -v`.

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
cc2flash devices
cc2flash device-info
cc2flash backup backup.zip
```

Without any setup and before `adbd` starts, the stock camera always exposes its
USB ADB function and appears in `adb devices` as `Ucamera001 offline`. The client
treats that exact offline state like an absent transport for both startup
mechanisms; it does not treat ambiguity, authorization errors, or other ADB
failures the same way.

If ADB is already online, `backup` proceeds without a normal-HID write. To start
the installed daemon only for this boot, use the separate command:

```sh
cc2flash start-adb
cc2flash backup backup.zip
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
stock `offline`, and temporary absence for up to 30 seconds. It then verifies
root ADB and stops; `backup` is a separate read-only invocation. Other ADB
errors and earlier upload failures remain fatal. Override the wait only when
necessary:

```sh
cc2flash start-adb --timeout 60
```

This route does not install a persistent file or require a restart. The vendor
handler nevertheless calls `sync`, and the running stock firmware may already
have pending JFFS2 changes, so it is not a guarantee that unrelated live-system
writes never reach flash. It specifically avoids the known `system.sh` change.

For optional persistent ADB, obtain online root ADB and preserve a backup first:

```sh
cc2flash start-adb
cc2flash backup backup.zip
cc2flash install-adb-startup --backup backup.zip
```

If installing the erase fix too, install it before optional persistent ADB so
its space budget takes priority. See [startup hooks](../docs/STARTUP-HOOKS.md).

The installer requires `ENABLE-ADB` (or explicit `--yes`), validates the backup
and live camera, checks clean config space, stages/readbacks files in tmpfs,
and installs the shared runner plus `enabled/90-adb.sh` through ADB. It verifies
persistent bytes and permissions. Different contents at `system.sh` or the
selected `enabled/90-adb.sh` path are refused. Unrelated regular hooks in
`enabled/` remain and execute in filename order. It does not use destructive HID
file upload. Restart manually after
successful installation.

`start-adb` is a no-op when the selected ADB device is already online.
`backup` never invokes either command automatically. Offline ADB requires the
temporary `start-adb` command, not the persistent installer.

`backup` requires the exact recovered partition sequence and sizes:

| MTD | Start | Size | Name |
|---|---:|---:|---|
| mtd0 | `0x000000` | `0x040000` | boot |
| mtd1 | `0x040000` | `0x150000` | kernel |
| mtd2 | `0x190000` | `0x158000` | root |
| mtd3 | `0x2e8000` | `0x4e8000` | system |
| mtd4 | `0x7d0000` | `0x010000` | hwconfig |
| mtd5 | `0x7e0000` | `0x020000` | config |

It attempts at most five complete reads and requires three **consecutive**
byte-identical 8 MiB images. A three-of-five majority is insufficient. Only
after that stability gate does it hash the 256 KiB `boot` partition and compare
it with the built-in known SHA-256:

```text
5602ec961b4410ccceea0d4910e4fa768c6998bd4ba86143ba50855bdd0b7a54
```

An unknown boot hash stops the command after the three matching reads and
prints the exact observed hash. After independently reviewing it, that one
value can be accepted explicitly:

```sh
cc2flash backup --accept-bootloader-sha256 <observed-sha256> backup.zip
```

The supplied value must exactly match the newly observed hash. Only then does
the client publish `backup.zip`. The ordinary ZIP contains exactly:

| Member | Purpose |
|---|---|
| `flash.bin` | accepted raw 8 MiB flash image |
| `manifest.json` | `cc2flash-backup-v2` acquisition evidence |

The manifest records the acquisition counts, hashes, partition map, boot
fingerprint, and acceptance basis. Both members are completed and CRC-checked
under a temporary name before one same-filesystem create-if-absent hard link
exposes the final ZIP. Publication cannot replace an archive that appears
concurrently, and an interruption cannot publish only the image or only its
evidence. Standard ZIP tools can extract `flash.bin` for independent inspection.

### Hardware-recovery interoperability

Pass the unmodified ZIP directly to the hardware-recovery builder; its embedded
three-consecutive-read evidence satisfies that tool's physical-read gate:

```sh
cc2flash inspect-image backup.zip
cc2flash build-image backup.zip
```

The builder writes
`backup-cc2-recovery/cc2-camera-recovery.bin`. It does not alter or replace
`backup.zip`. Preserve that original archive: it remains the acquisition
evidence required by restore.

Validate the resulting pair without opening USB:

```sh
cc2flash restore --dry-run \
  backup-cc2-recovery/cc2-camera-recovery.bin \
  --backup backup.zip
```

This proves that the candidate is a full restore image and is byte-identical to
the preserved camera backup everywhere except hardware recovery's audited
`0x463000–0x46afff` SquashFS patch window and
`0x7e0000–0x7fffff` config partition. The same compatibility check runs again
before `restore` opens USB, preventing a recovery image from another camera
or an image with unsupported additional edits from being written.

If ADB lists more than one device, pass the camera serial explicitly:

```sh
cc2flash backup --serial Ucamera001 backup.zip
```

The exact serial exposed by ADB may differ; use `cc2flash devices` to inspect it.

## Restore workflow (physically validated on the supported camera)

Root ADB must already be online; use the separate `start-adb` command if needed.
Connect only one ADB device and one normal camera HID interface, with matching
serials. See [SFC restore preparation](SFC-RESTORE-PREPARATION.md) for the exact
kernel gate, runtime pointer derivation, failure behavior, and hardware evidence.

Validate a candidate without opening USB:

```sh
cc2flash restore --dry-run fixed.bin --backup backup.zip
```

A full restore requires exactly `0x800000` bytes.  It also rejects an image
whose word at `0x7f8000` is the persistent upgrade magic, because that image
would return to upgrade mode on every reboot.

Restore requires the separately preserved three-read backup and its v2
manifest.
The replacement must match that backup outside the two hardware-recovery
regions documented above.
The transport parses each nonfinal ACK as the next expected absolute packet and
aborts if U-Boot requests retransmission, which this research client does not yet
implement. The command currently exists as:

```sh
cc2flash restore fixed.bin --backup backup.zip
```

Without correcting the live SFC erase size, the stock flag operation can damage
an occupied JFFS2 node without entering the bootloader. The integrated command
now validates and applies that temporary preparation before sending the flag.
The original failure, manual investigation, and successful end-to-end automated
validation are recorded in [PHYSICAL-VALIDATION.md](PHYSICAL-VALIDATION.md) and
[SFC-RESTORE-PREPARATION.md](SFC-RESTORE-PREPARATION.md).

The command prints the complete target range and hashes, then requires the
literal confirmation `RESTORE-CC2` for every real restore. Its phases are:

1. Validate `fixed.bin` and both members of `backup.zip` locally.
2. Require the known kernel, three consecutive identical live reads, and a
   match to the preserved backup outside the audited recovery regions. Validate
   root/MTD geometry, kernel symbols/instructions, and the runtime SFC pointer.
   Sync, set the internal erase size to `0x1000`, and verify it before sending
   the stock 8-byte upgrade flag through normal Linux HID. No manual config
   erase or process suspension is performed; `0x4000` is not restored in RAM.
3. Wait for bootloader HID `a108:ff08`.
4. Transfer a 128-byte update header plus the image in numbered packets.
5. Require the bootloader's whole-image MD5 success indication.
6. Wait without removing power while the bootloader erases, writes, and reboots.
7. Ensure root ADB is online. If it is offline, ask interactively whether to
   start `/bin/adbd` temporarily through normal HID; only an explicit `y` or
   `yes` sends that command. Then acquire three consecutive identical flash
   images over ADB.
8. Require every boot-stable byte from `0x000000` through the end of HWCONFIG at
   `0x7dffff` to match `fixed.bin`. Report whether config also stayed exact;
   serial-only images legitimately create default config files during this boot.

The bootloader reports MD5 acceptance **before** erase/write and offers no
post-write status or readback. ADB readback is therefore mandatory for a successful USB restore.

Consent to the flash write and consent to start ADB are separate. Both require interactive confirmation. Empty or negative input, and non-interactive stdin, send
no ADB-start HID command and leave the completed restore explicitly
unverified.

`--adb-timeout` is one deadline shared by the initial bounded ADB probe and, if
the temporary start is explicitly confirmed, the post-HID wait for ADB to come
online. Once ADB is online, stable post-write verification begins as a separate
operation with the ordinary per-command timeouts; the option does not claim to
bound up to five complete flash reads.

Do not unplug the camera after the MD5 response. A failed transfer before the
MD5 check does not erase flash, but the persistent flag may leave the camera in
bootloader mode; reconnecting the bootloader and retransmitting a full known
image is then the intended recovery path.

## Current limits

- Temporary ADB startup, binary-safe partition pulls, and a complete 8 MiB
  backup have been exercised on a physical camera from Windows. One first-run
  acquisition failed its equality gate while live state was settling; a later
  invocation produced matching full reads. The client now requires three
  consecutive matches within five attempts.
- Bootloader data ACK parsing now matches the disassembly for in-order packets,
  and one physical in-order transfer completed all 2,742 packets. Automatic
  retransmission remains absent and was not exercised.
- The bootloader full-flash erase/write, normal reboot, live video, and
  independent three-consecutive-read verification succeeded on one camera.
  The manual RAM-patched trigger and the later integrated automatic preparation
  both succeeded. The integrated run completed the full write, normal reboot,
  and required three-read verification.
- The stock Linux SFC driver advertises a 16 KiB erase size while its erase
  routine has opcode cases for 4, 32, and 64 KiB only. A boot-specific live RAM
  change to the internal size made 4 KiB sector erases work, confirming the
  mismatch. Restore now validates and applies that temporary preparation; it
  is not a persistent kernel fix and resets on reboot.
- Persistent ADB installation requires a backup and online ADB, refuses different
  contents at `system.sh` or the selected `enabled/90-adb.sh` path, and verifies
  installed contents. Unrelated regular enabled hooks remain and execute. The temporary upload-command
  route deliberately relies on a vendor command-injection bug. That temporary startup has now been
  hardware-verified, but the vendor handler's `sync` caveat remains.
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

The 106 tests exercise the 57-entry normal command catalog, all configuration and
upload mappings, command builders, U-Boot frame types/ACK decoders, frame
vectors, checksums, image headers, packet numbering, partition validation, ADB
absence/offline/error classification, three-consecutive-of-five acquisition,
boot-hash gating, create-if-absent ZIP publication, strict v2 manifest parsing
including JSON decoder-limit failures,
unsupported-compression rejection, both explicit ADB startup commands,
hardware-recovery region compatibility, boot-stable post-write comparison,
separately confirmed temporary ADB startup after a serial-only restore, and
pre-USB wrong-unit rejection,
bounded/validated availability timeouts, expected final-commit
failure/disconnection, the Windows `error: closed`
transport handoff, legacy text-shell parsing, ordered binary MTD pulls and
temporary-file cleanup, CLI mutation guards, dynamic SFC pointer derivation,
exact-kernel/instruction/value gates, transport matching, guarded RAM write and
readback failures, restore ordering, and the HID state machines without opening
a device.

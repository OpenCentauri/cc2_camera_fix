# On-printer HID installer: technical report

## Implementation scope

`cc2camera-hid` runs on the CC2 and installs two startup scripts through the
camera's HID updater. It does not export a backup, compare identity against a
backup, or check safely writable JFFS2 space. Installation requires explicit
`--accept-no-backup-space-risk` consent: filesystem exhaustion or interruption
can leave the camera unbootable and require programmer recovery. Near
exhaustion, ordinary boot-time writes can also trigger failure; avoiding a
preparatory reboot is part of this workflow.

Before persistent writes, the worker checks firmware fingerprints, partitions,
mounts, file types, permissions, identity-file presence and configuration
stability. It compares installed bytes with the embedded scripts. It writes no
firmware image and exposes no arbitrary-file or arbitrary-command interface.

## Printer environment

The observed printer runs BusyBox 1.27.2 ash on TinaLinux 5.4.61, ARMv7 little
endian with two Cortex-A7-class processors and approximately 108.6 MiB RAM. USB
device nodes are available under `/dev/bus/usb`; no `hidraw` nodes were present
with either observed camera. The static binary accesses Linux usbfs without
requiring ADB, Python or shared libraries on the printer.

## Observed 30D USB signature

The observed 30D has a complete 1,005-byte (`0x3ed`) USB descriptor snapshot
through the printer's sysfs. It declares one configuration of 987 bytes
(`0x3db`), four interface numbers, and two UVC function associations. Windows
presented two cameras for the 30D, versus one for the observed 30B. These are
USB functions, not evidence of two physical image sensors.

| Attribute | Observed 30D value |
|---|---|
| VID:PID | `a108:2240` — shared with the 30B |
| `bcdUSB`, `bcdDevice` | `0200`, `0414` |
| Device class/subclass/protocol | `ef/02/01` |
| Manufacturer | `Linux Foundation` |
| Product | `Multi Composite Double Uvc Gadget` |
| First UVC function | Control interface 0; streaming interface 1, alternate settings 0/1; IN endpoint `0x81` at alternate 1 |
| Second UVC function | Control interface 2; streaming interface 3, alternate settings 0/1; IN endpoint `0x82` at alternate 1 |
| Endpoint attributes / maximum packet / interval | `05` / `03fc` (1,020 bytes) / `01`, for both streaming endpoints |
| HID / ADB interfaces | Neither is declared in the sole configuration |

The absence of an ADB USB interface is distinct from an exposed ADB interface
whose daemon is not running. The observed 30B exposes both HID and ADB
interfaces even when the ADB daemon is not running.

The native classifier matches the complete device/configuration header, total
length, manufacturer/product strings, and ordered standard function/interface/
endpoint descriptors above. Every intervening descriptor must be a well-bounded
class-specific UVC interface descriptor (`0x24`). Its format/frame payload bytes
are not matched. This is an observed USB signature, not authentication or a
full-firmware fingerprint, and does not promise recognition of every 30D
firmware revision. USB addresses, speed, serial strings and the currently
selected video alternate settings are not used for recognition.

Recognition happens using sysfs, before opening `/dev/bus/usb`, claiming an
interface, or sending a version query. `inspect` reports that the signature
matches the observed 30D and the patch does not apply to that revision.
`install`/`verify` refuse it. Missing HID, a generic double-UVC device, a
partial signature, malformed descriptors, or multiple matching-ID devices cannot
produce that reassurance. Unknown devices still receive the ordinary unsupported
result.

Software coverage uses generated topology vectors with synthetic class-specific
payloads. It checks signature mutations, truncation, malformed lengths, missing
strings, wrong active configuration, mixed 30B/30D discovery and absence of USB
access for every command on a recognized 30D. Live execution of the 30D
classifier on the printer remains unverified.

The read-only collection command for the observed topology was:

```sh
busybox hexdump -C /sys/bus/usb/devices/1-1.1/descriptors
```

That USB path is specific to the supplied connection and must be discovered
again if topology changes. The sysfs manufacturer/product attributes supplied
the string values; the binary descriptors contain string indices only.

## Protocol and transport

The implementation follows the normal-mode format in
[PROTOCOL.md](../usb-maintenance/PROTOCOL.md): report ID 1, 1024-byte reports,
`5a 5a` magic, the recovered right-shifting `0x1021` CRC, little-endian fields,
and at most 1010 payload bytes per packet. The shell payload is bounded to 32
KiB, though the camera's uploader allocates its stock 16 MiB + 512-byte receive
buffer. A small host executable does not reduce that camera-side allocation.

The Linux usbfs backend parses the active configuration, requires exactly one
HID interface and interrupt IN endpoint, and uses the discovered interrupt OUT
endpoint or HID `SET_REPORT` on endpoint zero when there is no OUT endpoint. It
compares the opened device's descriptors with those used for selection, claims
only the HID interface, and detaches only a driver named `usbhid` if necessary.
It issues no USB reset, configuration/alternate-setting changes, or
video-interface detach. Synchronous USB transfers use five-second timeouts. The
ABI definitions are narrowly scoped to Linux ARM, AArch64, and x86-64.

`inspect` sends only `0x0001` (version query). `install` and `verify` use:

1. `0x3000`, `0x3110`, numbered `0x3200` packets and `0x3300` to stage an
   installer at `/tmp/.cc2-hid-<16-hex-session>.sh`.
2. The same upload sequence with one newline and an internally generated target
   ending in `;/bin/sh${IFS}/tmp/.cc2-hid-<session>.sh&`.
3. `0x0001` queries for the exact current session's terminal status, for at most
   90 seconds, with no upload retries.

The [reconstructed commit
handler](../usb-maintenance/source-reconstruction/hid_update_reconstructed.c)
interpolates the target into `rm` before attempting its literal `fopen`. The
background shell starts, and the embedded directory separators make the literal
open fail. Only status 1 is expected for this launch commit. A timeout or
malformed commit reply is an error, not accepted evidence of success. On that
failure path, the uploader is left in state 5; initialization does not reset it.
Hence only one worker launch per camera boot is supported. The version-query
handler remains separate from the upload state machine: `inspect` sends only
that query, changes no uploader state and needs no reboot before `install`. A
first installation uses the already-running camera; no preparatory reboot is
required. After successful installation, a full camera power cycle activates the
startup scripts and resets the uploader for verification.

The worker uses the first line of camera `/tmp/version.txt` as a temporary
result channel. That handler returns at most 23 bytes; a 16-hex-digit random
session, colon, and status fit. `BUSY` is nonterminal; `DONE`/`SAME` confirm
installation comparisons; `LIVE` confirms verification; `FAIL` means preflight
refusal and `PART` means persistent writes started but did not finish
successfully. Results from another session or another operation cannot authorize
success. The original version file is restored two minutes after the worker
finishes; if it was absent, the newly created file is removed. Status records
are published by rename so a query cannot observe a partially written record.
Unexpected replacements are preserved along with staging diagnostics.
Session-bound results and normal camera-feed operation after installation and
verification have been observed. Cleanup timing has not been independently
confirmed.

The first staging upload precedes camera-side checks. It relies on the analyzed
stock `/tmp` layout and a fresh random destination; the worker then requires
`/tmp` to be tmpfs before additional staging/status changes. USB identity alone
does not authenticate firmware. Firmware fingerprints guard persistent writes,
not the preceding temporary upload or shell-launch mechanism. This is not an
adversarial-device security boundary.

## Persistent writes and verification

The worker checks the exact known kernel MD5 and recovered `/bin/hid_update` MD5
documented in [EVIDENCE.md](../usb-maintenance/EVIDENCE.md). MD5 is an
interoperability fingerprint, not cryptographic authentication. It checks the
complete expected MTD map, the configuration mount, regular nonempty identity
file, and managed destinations. Three equal raw configuration digests, plus a
repeat immediately before writing, detect observed concurrent changes. They are
not an exported backup or a guarantee against a later concurrent writer.

Canonical hook bytes come from `cc2camera.startup_payloads`; a generation check
prevents the native copy from drifting. Both payload files are compared against
embedded MD5 fingerprints in RAM before installation. Differing managed contents
are refused unless install explicitly uses `--overwrite-managed-scripts`.
Unexpected permissions, symlinks and incomplete-installation paths remain
refused. Existing unrelated regular enabled hooks remain and execute under the
canonical runner.

The worker copies each missing or explicitly replaced file to a fixed
refused-if-present temporary configuration path, sets mode 755, compares bytes,
syncs, renames, and syncs. The erase hook is installed before the runner. Final
comparisons check both files. No direct flash writes, partition
erasure/remounting, or live kernel modifications occur during installation.
Failure preserves partial persistent files; there is no rollback that would
consume more JFFS2 records.

After restart, `verify` stages the same bounded worker in verification mode. It
compares canonical files and reads the known kernel instructions, symbols,
validated SFC pointer and master erase-size field. It reports `LIVE` only when
that field is `0x1000` and the pointer remains stable. It never applies the
correction itself. This is a camera-side verification report over HID, not a
host-side independent full-flash readback or physical erase-pressure test.

## Validation status

Software tests cover generation parity, framing/CRC, descriptor selection,
consent and argument refusal, upload failure ordering/no retries, session-bound
status acceptance, shell fingerprint refusals, unknown files and symlinks,
configuration changes, partial writes, readback mismatches and idempotency.
Synthetic fixtures contain no proprietary firmware or real device identity.

The workflow compiles a static ARMv7 binary and runs tests under QEMU in
addition to host tests.

Physical 30B tests on 2026-09-09 used the on-printer binary at commit
`ce33161c3a02a7e242cfe313d69c549bd97a96f0`:

- Installation with `--overwrite-managed-scripts` progressed from `BUSY` to a
  matching-session `DONE`. The worker completed its checks and verified both
  installed script contents and mode 755 after persistent writes.
- The subsequent verification log progressed from `BUSY` to a fresh matching-
  session `LIVE`. This confirms the worker's installed-file comparisons, kernel
  fingerprints/instruction/symbol checks, validated pointer, and live erase-size
  field of `0x00001000`. The verification worker reads that field and does not
  apply the correction.

This is physical evidence for installation and subsequent live verification
through the printer's USB HID transport on the tested, previously modified 30B.
It is not an independent full-flash readback, a clean stock-camera installation
test, or an erase-pressure/endurance test. Normal camera-feed operation was also
observed after installation and verification. Status-file cleanup timing has not
been separately confirmed. The route remains experimental and retains its
no-backup/no-space-admission risks.

Earlier testing after a printer shell `reboot` timed out on the first upload
packet. A full power cycle allowed uploads again. A printer reboot must not be
assumed to reset camera power or its updater state; verification after
successful installation requires a full power cycle after worker cleanup. A
failed attempt requires diagnosis before deciding whether another boot is safe.

The existing hook's separate physical evidence is documented in
[STARTUP-HOOKS-VALIDATION.md](../docs/STARTUP-HOOKS-VALIDATION.md).

## Observed 30B USB snapshot and version-query limitation

The observed 30B connected to the printer has a 1,794-byte (`0x702`) descriptor
snapshot. Its single configuration is `0x6f0` bytes, with six interface numbers.
The descriptor-byte SHA-256 is
`732b7795d9f6945fb32df6f21e4ef85a38c640f396c888b51474b96a7db5e62a`. This digest
identifies the supplied metadata snapshot, not a firmware image.

| Attribute | Observed 30B value |
|---|---|
| VID:PID / device revision | `a108:2240` / `0090` |
| Manufacturer | `Ingenic Semiconductor Co.,Ltd` |
| Product | `Ingenic HD Web Camera` |
| UVC functions | Two, covering interfaces 0/1 and 2/3 |
| HID | Interface 4, class/subclass/protocol `03/00/00`; interrupt IN `0x84`, OUT `0x01`, each maximum packet 1,024 bytes |
| ADB | Interface 5, class/subclass/protocol `ff/42/01`; bulk IN `0x85`, OUT `0x02`, each maximum packet 512 bytes |
| Printer driver observation | No bound driver on HID/ADB; no `/dev/hidraw*` nodes |

Both observed revisions declare two UVC functions. The observed Windows
presentation of one camera for the 30B and two for the 30D is not a reliable
USB-level discriminator. The 30D classifier additionally requires its revision,
strings and complete standard descriptor topology, including no HID/ADB.

On physical hardware, `inspect` selected HID interface 4 and completed a version
exchange with valid framing/CRC, status 1 and an empty payload. The
reconstructed handler returns this when opening or reading `/tmp/version.txt`
fails. That file lives in camera RAM; its absence is allowed.

The host accepts only status 1 with an empty payload as unavailable metadata.
Other nonzero statuses, empty status-0 data, payloads longer than 23 bytes and
non-printable data refuse before upload. Failure diagnostics include the status,
payload length, escaped ASCII and a hex preview capped at 64 bytes. Polling also
allows the exact unavailable reply while the worker starts, within its 90-second
deadline. It never treats it as success or retries an upload.

## Worker refusal diagnostics

The worker encodes named failures as `Fnn` (before persistent installation
writes) or `Pnn` (after writes began). The shared `failure-reasons.txt` catalog
drives shell generation and host decoding. Unknown codes remain failures; legacy
`FAIL` and `PART` retain their refusal and partial-write meanings. Only a result
carrying the current session token can complete an operation.

Managed-file diagnostics distinguish the starter (`system.sh`) and erase hook,
then file type, stat failure, permissions, different contents or comparison-tool
failure. Missing files are allowed, as required for installation on stock
cameras. Existing exact scripts are preserved. With
`--overwrite-managed-scripts`, differing regular mode-755 scripts can be
replaced. Originals are kept in camera RAM under
`/tmp/.cc2-old-scripts-<session>/fix` and `runner`, outside worker cleanup, and
compared again before replacement. They are lost on camera reboot and are not an
exported backup. Verification always requires the exact expected contents.

`--verbose` logs complete outgoing and incoming HID reports, including padding,
upload payloads, malformed replies and transport errors. It adds no exchanges or
retries and does not capture camera shell output or video traffic. An outgoing
log entry records an attempted transfer, not proof of receipt.

## Build and release checks

Build on a development computer. There are no Cargo dependencies; Python checks
that embedded scripts match `cc2camera.startup_payloads`. CI uses Rust 1.85.1
and the static `armv7-unknown-linux-musleabihf` target.

On Linux with Rust and an ARM GNU linker installed:

```sh
python on-printer/generate_payload.py --check
cargo test --locked --manifest-path on-printer/Cargo.toml
rustup target add armv7-unknown-linux-musleabihf
CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=arm-linux-gnueabihf-gcc \
  cargo build --locked --manifest-path on-printer/Cargo.toml \
  --release --target armv7-unknown-linux-musleabihf
```

CI runs native and QEMU ARM tests, checks for absence of an ELF interpreter and
shared-library dependencies, and exercises help and missing-consent refusal.
Push/PR builds provide the executable and SHA-256 file as a workflow artifact.
The manually published-release workflow calls the ARM build and attaches both
files alongside the Windows executable after both builds succeed.

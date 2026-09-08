# On-printer HID installation: evidence and limits

## Accepted scope and risk exception

The developer explicitly requested a separate small tool that runs from a CC2
shell without ADB or Python, installs the camera fix through HID without
unplugging the camera, and trades the conservative target-space check for an
explicit risk of programmer recovery. This experimental workflow has no exported
backup or backup-based identity comparison. It installs independently written
canonical configuration hooks, not a firmware image or another camera's data.
The CLI exposes this tradeoff as `install --accept-no-backup-space-risk`.

This exception applies only to this workflow. The ADB installer, backup, image
patching, and restore safety contracts remain in effect. The shell installer
retains mount/partition checks, known firmware fingerprints, regular-file checks,
unknown-hook refusal, configuration stability checks, and camera-side byte
comparisons. The erase hook itself retains every early-boot compatibility gate.
There is no generic force option or arbitrary-file/command interface.

A filesystem space report is not the existing clean-marker admission test.
Neither the printer's free space nor an acknowledgement from the uploader can
prove that the camera has enough safely writable JFFS2 space.

## Observed printer environment

The developer supplied this shell evidence:

- BusyBox 1.27.2 ash; neither ADB nor Python available;
- TinaLinux 5.4.61, ARMv7 little endian, two Cortex-A7-class processors, VFPv3/4;
- approximately 108.6 MiB total printer RAM, with approximately 60.4 MiB free
  after excluding buffers/cache in the supplied snapshot;
- no `/dev/hidraw*` nodes, but USB device nodes under `/dev/bus/usb`.

The developer subsequently identified the camera used for that snapshot as
EF-S7-V1.0.30D, not 30B. The absence of `hidraw` in this observation therefore
does not establish whether the printer exposes `hidraw` with a 30B connected.
The direct usbfs backend does not depend on that distinction.

This establishes an architecture and a candidate USB access mechanism. It does
not establish successful interface claiming, report transfer, camera-side worker
execution, or a working installation. USB descriptors must still be inspected on
hardware; the implementation discovers endpoint addresses instead of assuming
an interface number or address from the changing device-node numbers.

## Observed 30D USB signature

A developer-confirmed 30D supplied a complete 1,005-byte (`0x3ed`) USB descriptor
snapshot through the printer's sysfs. It declares one configuration of 987 bytes
(`0x3db`), four interface numbers, and two UVC function associations. The developer
also observes two cameras under Windows for the 30D, versus one for the 30B.
These are USB functions, not evidence of two physical image sensors.

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
whose daemon is not running. The developer confirms the 30B exposes both HID
and ADB interfaces even when ADB is not enabled. The existing stock 30B bootstrap
workflow also relies on that distinction.

The native classifier matches the complete device/configuration header, total
length, manufacturer/product strings, and ordered standard function/interface/
endpoint descriptors above. Every intervening descriptor must be a well-bounded
class-specific UVC interface descriptor (`0x24`). Its format/frame payload bytes
are not matched. This is an observed USB signature, not authentication or a
full-firmware fingerprint, and does not promise recognition of every 30D firmware
revision. USB addresses, speed, serial strings and the currently selected video
alternate settings are not used for recognition.

Recognition happens using sysfs, before opening `/dev/bus/usb`, claiming an
interface, or sending a version query. `inspect` reports that the signature
matches the observed 30D and the patch does not apply to that revision.
`install`/`verify` refuse it. Missing HID, a generic double-UVC device, a partial
signature, malformed descriptors, or multiple matching-ID devices cannot produce
that reassurance. Unknown devices still receive the ordinary unsupported result.

Software coverage uses generated topology vectors with synthetic class-specific
payloads. It checks signature mutations, truncation, malformed lengths, missing
strings, wrong active configuration, mixed 30B/30D discovery and absence of USB
access for every command on a recognized 30D. Live execution of this classifier
on the printer remains unverified. No firmware disassembly is needed to establish
this USB-level distinction.

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
and at most 1010 payload bytes per packet. The shell payload is bounded to
32 KiB, though the camera's uploader allocates its stock 16 MiB + 512-byte receive
buffer. A small host executable does not reduce that camera-side allocation.

The Linux usbfs backend parses the active configuration, requires exactly one
HID interface and interrupt IN endpoint, and uses the discovered interrupt OUT
endpoint or HID `SET_REPORT` on endpoint zero when there is no OUT endpoint.
It compares the opened device's descriptors with those used for selection,
claims only the HID interface, and detaches only a driver named `usbhid` if
necessary. It issues no USB reset, configuration/alternate-setting changes, or
video-interface detach. Synchronous USB transfers use five-second timeouts.
The ABI definitions are narrowly scoped to Linux ARM, AArch64, and x86-64.

`inspect` sends only `0x0001` (version query). `install` and `verify` use:

1. `0x3000`, `0x3110`, numbered `0x3200` packets and `0x3300` to stage an
   installer at `/tmp/.cc2-hid-<16-hex-session>.sh`.
2. The same upload sequence with one newline and an internally generated target
   ending in `;/bin/sh${IFS}/tmp/.cc2-hid-<session>.sh&`.
3. `0x0001` queries for the exact current session's terminal status, for at most
   90 seconds, with no upload retries.

The [reconstructed commit handler](../usb-maintenance/source-reconstruction/hid_update_reconstructed.c)
interpolates the target into `rm` before attempting its literal `fopen`. The
background shell starts, and the embedded directory separators make the literal
open fail. Only status 1 is expected for this launch commit. A timeout or malformed
commit reply is an error, not accepted evidence of success. On that failure path,
the uploader is left in state 5; initialization does not reset it. Hence only one
attempt per fresh boot is supported. The version-query handler remains separate
from the upload state machine.

The worker uses the first line of camera `/tmp/version.txt` as a temporary result
channel. That handler returns at most 23 bytes; a 16-hex-digit random session,
colon, and status fit. `BUSY` is nonterminal; `DONE`/`SAME` confirm installation
comparisons; `LIVE` confirms verification; `FAIL` means preflight refusal and
`PART` means persistent writes started but did not finish successfully. Results
from another session or another operation cannot authorize success. The original
version file is copied back two minutes after the worker finishes. This behavior
and its effect on printer software are hardware-unverified.

The first staging upload precedes camera-side checks. It relies on the analyzed
stock `/tmp` layout and a fresh random destination; the worker then requires
`/tmp` to be tmpfs before additional staging/status changes. USB identity alone
does not authenticate firmware. Firmware fingerprints guard persistent writes,
not the preceding temporary upload or shell-launch mechanism. This is not an
adversarial-device security boundary.

## Persistent writes and verification

The worker checks the exact known kernel MD5 and recovered `/bin/hid_update`
MD5 documented in [EVIDENCE.md](../usb-maintenance/EVIDENCE.md). MD5 is an
interoperability fingerprint, not cryptographic authentication. It checks the
complete expected MTD map, the configuration mount, regular nonempty identity
file, and managed destinations. Three equal raw configuration digests, plus a
repeat immediately before writing, detect observed concurrent changes. They are
not an exported backup or a guarantee against a later concurrent writer.

Canonical hook bytes come from `cc2camera.startup_payloads`; a generation check
prevents the native copy from drifting. Both payload files are compared against
embedded MD5 fingerprints in RAM before installation. Unknown managed contents,
permissions, symlinks and incomplete-installation paths are refused. Existing
unrelated regular enabled hooks remain and execute under the canonical runner.

The worker copies each missing file to a fixed refused-if-present temporary
configuration path, sets mode 755, compares bytes, syncs, renames, and syncs.
The erase hook is installed before the runner. Final comparisons check both
files. No direct flash writes, partition erasure/remounting, or live kernel
modifications occur during installation. Failure preserves partial persistent
files; there is no rollback that would consume more JFFS2 records.

After restart, `verify` stages the same bounded worker in verification mode.
It compares canonical files and reads the known kernel instructions, symbols,
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

The new workflow compiles a static ARMv7 binary and runs tests under QEMU in
addition to the host tests. These checks cannot substitute for the following
physical validation:

- inspect the actual camera descriptors and complete a version query from CC2;
- establish that the printer's camera service tolerates HID access;
- observe the expected launch-commit failure and current-session status response;
- compare installation results and verify activation after restart;
- observe version restoration and normal camera feed operation;
- exercise any on-device failure investigations deliberately, with programmer
  access and preserved same-camera data where available.

The existing hook's physical evidence in
[STARTUP-HOOKS-VALIDATION.md](STARTUP-HOOKS-VALIDATION.md) does not validate this
new transport, installer, or otherwise-stock hook-only boot behavior. User
instructions therefore label this route experimental and hardware-unverified.

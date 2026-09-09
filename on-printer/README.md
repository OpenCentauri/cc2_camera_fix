# Install the camera fix from a CC2 shell

`cc2camera-hid` is an **experimental** way to protect a
working affected stock camera while it stays connected inside the printer.
It runs on the printer, using USB HID directly. The static ARMv7 executable
needs neither ADB, Python, `hidraw`, nor additional shared libraries.
Installation with managed-script replacement and subsequent live verification
have succeeded on the tested 30B camera. See the [validation scope](../docs/ON-PRINTER-HID.md#validation-status).

The [computer-based USB route](../README.md#working-camera-usb-prevention)
provides an exported backup and a conservative clean-space check. Prefer that
route when you can connect the camera to a computer.

## Risk and prerequisites

This installer **does not export a backup or check that enough safely erased
camera configuration space remains**. Even a small write can exhaust the
camera's damaged filesystem. A failed or interrupted installation can leave the
camera unable to boot and require an external SPI programmer. Without a backup,
recovery depends on being able to read the camera's own identity data with that
programmer; recovery is not guaranteed. Never substitute another camera's dump.
The available space reported by `df` on the **printer** says nothing about this
risk on the **camera**.

Use this route only if you accept that tradeoff to avoid unplugging the camera
and making a cable. The tool requires that acceptance explicitly on installation.
The backup-based tools keep their existing requirements.

You need:

- a working affected Ingenic T23 camera, with the known stock kernel and HID
  updater; this does not support the newer TX5110 camera or recover a camera
  that no longer boots;
- root shell access to the CC2 printer (obtaining this access is outside this
  guide), and a way to copy the executable onto its USB drive or filesystem;
- a 32-bit ARMv7 Linux printer with `/sys/bus/usb/devices` and
  `/dev/bus/usb`; the initial target is the reported CC2 TinaLinux 5.4.61 system;
- an idle printer, with no print running and no other camera-maintenance tool
  using the HID interface.

Restart the **idle** printer before starting an installation or verification
attempt, so the camera uploader starts in its ordinary state. Do not use
`cc2camera start-adb` or another HID uploader first. During an attempt, do not
interrupt power. The program never resets USB, changes its configuration,
detaches a video interface, reboots the printer, or writes a firmware image.
If a HID kernel driver is present, it temporarily detaches only `usbhid` on the
selected HID interface and requests reattachment when it exits normally.
Physical testing must establish whether the stock printer software tolerates
this access while it owns the camera stream.

## Obtain and inspect the executable

For a version containing this tool, download `cc2camera-hid-armv7-linux` and its
`.sha256` file from the repository's [GitHub Releases](https://github.com/OpenCentauri/cc2_camera_fix/releases).
Before a release is available, the **On-printer HID tool** workflow on the PR
provides the same files in its `cc2camera-hid-armv7-linux` artifact. Extract that
artifact on your computer and place the two files at the root of a USB drive.
Do not execute a binary from a failed build.

On a CC2 where that drive is mounted at `/mnt/exUDISK`:

```sh
cd /mnt/exUDISK
sha256sum -c cc2camera-hid-armv7-linux.sha256
cp cc2camera-hid-armv7-linux /tmp/cc2camera-hid
chmod 755 /tmp/cc2camera-hid
/tmp/cc2camera-hid --help
/tmp/cc2camera-hid inspect
```

If `sha256sum` is unavailable on the printer, verify the checksum on your
computer before copying the binary. Adjust the drive mount path if necessary.
Copying to `/tmp` avoids execution permissions on the USB drive; the executable
must be copied there again after a printer restart.

`inspect` recognizes the observed EF-S7-V1.0.30D USB signature from descriptors
and manufacturer/product strings. For that signature it reports that the patch
does not apply, exits successfully, and never opens the USB device node or sends
a camera command. `install` and `verify` refuse that signature without USB access.
The match requires revision `0414` and the specific two-function UVC layout;
shared USB IDs or missing HID alone do not establish a 30D. Unknown layouts
remain unsupported. Multiple matching-ID cameras are refused before classification.

For a supported HID camera, `inspect` sends only the version-query command. It does
not upload scripts or change camera files. Expect exactly one camera, its
selected HID interface and endpoints, and either a printable version response or an unavailable-version message. USB
identification and a version reply alone do **not** establish compatible
firmware: the camera-side installer checks fingerprints before persistent writes.
Stop if inspection reports an error, multiple cameras, or bootloader mode.
Do not manually unbind arbitrary USB drivers to bypass a refusal.

The exact status-1 reply with no payload means the camera could not open/read
its temporary version file. This is accepted as unavailable metadata, not proof
of compatible firmware. Inspection does not create that file.

If another version reply fails validation, the error includes its status, payload
length, and escaped text/hex bytes (at most 64 bytes of preview). Paste that
diagnostic when reporting an inspection failure. Control bytes are escaped so
they cannot act as terminal commands. The version check still refuses the
operation before any upload; these diagnostics do not bypass validation.

## Replacing existing managed scripts

`install --accept-no-backup-space-risk --overwrite-managed-scripts` explicitly
allows replacing differing contents of `/etc/conf.d/system.sh` and
`/etc/conf.d/enabled/10-erase-fix.sh`, including scripts from older tool versions.
This can remove custom behavior in those two files. Other enabled scripts are
preserved. Without this flag, differing contents remain a refusal.

Only regular, nonsymlink files with mode 755 may be replaced. All firmware,
identity, mount, partition, stability and readback checks remain. The existing
no-backup/no-space-check risk consent is still required. `inspect` and `verify`
reject this flag; verification always requires the tool's exact expected bytes.

Before writes, existing originals are copied to camera RAM under
`/tmp/.cc2-old-scripts-<session>/fix` and `runner`. They survive worker cleanup
but are lost when the camera reboots; they are not an exported or durable backup.
Each replacement rechecks the original against its RAM copy and uses a verified
temporary file followed by rename. Successful installation checks the final
contents strictly. Partial-write failures do not automatically roll back.

## Install, then verify after restart

The following command **can write the camera's persistent configuration**.
It carries the recovery risk described above:

```sh
/tmp/cc2camera-hid install --accept-no-backup-space-risk
```

The tool uploads a temporary installer to camera RAM. The installer checks the
kernel and HID updater fingerprints, partition map, mounts, identity-file
presence, and stability of the raw configuration contents. By default it refuses
differing contents at either managed destination. Unexpected permissions are
always refused. It installs the canonical
`10-erase-fix.sh` hook first and `system.sh` runner last, comparing temporary and
final file contents. It preserves unrelated regular enabled hooks and refuses
non-regular entries. Exact existing managed files are left untouched.

The only successful installation message begins:

```text
Camera reports exact installed hook contents and permissions verified.
```

This confirms the camera-side file comparisons, **not activation on boot**.
After that message, wait at least two minutes, fully power-cycle the idle printer, copy
the executable back to `/tmp`, and run. A shell `reboot` may leave the camera
powered and its HID uploader in the previous state; it is not sufficient to
establish a fresh camera boot:


```sh
/tmp/cc2camera-hid verify
```

`verify` explicitly uploads and runs a temporary probe in camera RAM. It makes
no persistent changes and does not apply the RAM correction. It checks the
canonical installed files, known kernel instructions and symbols, and the live
erase-size field. Its success message is:

```text
Camera reports canonical hooks and the live erase correction verified for this boot.
```

Also check that the camera feed works. Report the tool output and whether the
feed survives installation/restart when physically validating this route.
Do not treat a passing offline test or a successful HID commit as physical
validation.

## If anything fails

Do not retry automatically or repeatedly. An incomplete installation is preserved
for diagnosis, including `.cc2-hid-fix` or `.cc2-hid-runner` temporary files in
camera configuration. The tool deliberately does not delete or overwrite these
files on a later attempt.

- A camera-side preflight refusal reports that persistent installation writes
  did not start. Resolve the refusal before attempting anything else.
- A partial-installation error means persistent writes started. Keep power on
  while deciding how to preserve the current camera data; restarting could make
  a nearly full camera filesystem unbootable.
- A USB error, malformed response, or timeout leaves the outcome unknown.
  A timeout does not stop a worker already running on the camera. Do not assume
  that nothing was written or that restarting is safe.
- If the camera no longer boots, use the
  [hardware recovery guide](../README.md#failed-camera-hardware-recovery),
  preserving its own flash contents first.

Both `install` and `verify` temporarily replace the camera's version-query
response with a session-specific status. Two minutes after the worker finishes,
it restores the original version file if one existed, or removes the file it
created. Symlinks, nonregular files and existing files larger than 4096 bytes
are refused before status writes. The command-launch mechanism leaves the
camera's HID **uploader** in an error state until the camera daemon restarts;
version queries remain usable. The program does not automatically restart that
daemon. Do not attempt another upload in the same boot.

## Build and software verification

Build on a development computer, not the printer. There are no Cargo dependencies.
Python is used only to generate/check embedded copies of the existing hook bytes.
The workflow uses Rust 1.85.1 and a static
[`armv7-unknown-linux-musleabihf` target](https://doc.rust-lang.org/rustc/platform-support/arm-linux.html).

On Linux with Rust and an ARM GNU linker installed:

```sh
python on-printer/generate_payload.py --check
cargo test --locked --manifest-path on-printer/Cargo.toml
rustup target add armv7-unknown-linux-musleabihf
CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=arm-linux-gnueabihf-gcc \
  cargo build --locked --manifest-path on-printer/Cargo.toml \
  --release --target armv7-unknown-linux-musleabihf
```

CI runs native and QEMU ARM tests, checks that the executable has no ELF
interpreter or shared-library requirements, and exercises help and missing-consent
refusal. It reports the actual binary size and publishes a checksummed artifact.
Manually published releases receive the same executable; the workflow does not
create releases.

See [implementation evidence and validation limits](../docs/ON-PRINTER-HID.md)
for the protocol, accepted exception, and outstanding hardware checks.

## Diagnostic logging

Add `--verbose` to any command to log every complete outgoing and incoming HID
report as hex on stderr, including padding, upload payloads and malformed replies.
Transport errors are logged too. TX records describe an attempted exchange, not
proof that the camera received it. Logging adds no queries or retries and does
not capture video traffic or the camera process's stdout/stderr.

```sh
./cc2camera-hid inspect --verbose > /tmp/cc2camera-inspect.log 2>&1
cat /tmp/cc2camera-inspect.log
```

Installation can also be logged with `install --accept-no-backup-space-risk
--verbose`. Only run that operation under the installation prerequisites above;
logging does not make it read-only or permit retrying in the same boot.

Worker failures include the check name and a compact status code. `Fnn` means
refusal before persistent installation writes; `Pnn` means writes had begun.
Both are failures, and only a result carrying the current session token is
accepted. Legacy `FAIL`/`PART` replies retain their failure meaning.

Missing managed files are allowed: a stock camera need not already have
`/etc/conf.d/system.sh` or `enabled/10-erase-fix.sh`. An existing canonical
starter with mode 755 is preserved, including when the erase hook is absent.
Managed-file failures identify `starter` (`system.sh`) or `erase-hook`, followed
by `type`, `stat`, `mode`, `content`, or `compare`. `content` means `cmp` reported
different bytes; `compare` means the comparison command failed. These diagnostics
do not authorize overwriting existing files.

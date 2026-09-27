# Install the camera fix from a CC2 shell

This tool installs the startup fix on a working, affected camera without
disconnecting it from the printer. You need root shell access to an idle CC2
and a USB drive to transfer the executable. Obtaining shell access is outside
this guide.

First [identify your camera](../README.md#first-identify-your-camera). This
route supports the affected 8 MiB 30B family, not the early 16 MiB 30B or the
30D. A camera that no longer boots needs [hardware recovery](../README.md#failed-camera-hardware-recovery).

**Experimental:** installation and verification have succeeded on a previously
modified 30B. First installation on an unmodified, supported camera still needs
volunteers. If you try it, please [open an issue](https://github.com/OpenCentauri/cc2_camera_fix/issues/new)
to share results and get help with problems or tool adjustments.

## Before installing

The installer writes two startup scripts to the camera. It does **not** make a
backup or check how much safely writable configuration space remains. If that
space is nearly exhausted, installing the scripts can leave the camera unable
to boot, requiring an external SPI programmer for recovery. Power loss during
installation can also cause failure.

Leaving an affected camera unchanged also consumes configuration space on each
boot. A camera close enough to exhaustion for these writes to cause failure
may have only a few normal boots left. Moving it to a computer for the
[USB procedure](../README.md#working-camera-usb-prevention) adds a boot that can
itself trigger failure. That procedure offers a backup and space check once
connected; this route avoids the extra boot and cable work, but is less tested.
The number of remaining boots cannot be determined by this tool.

**Keep the camera powered for its first installation. Do not reboot as a
preparation step.** If you have already attempted `install` or `verify` during
this camera boot, see [If a command fails](#if-a-command-fails) before doing more.

## Download and inspect

Download `cc2camera-hid-armv7-linux` and `cc2camera-hid-armv7-linux.sha256` from
[Releases](https://github.com/OpenCentauri/cc2_camera_fix/releases) and put both
files at the root of a USB drive. Insert it into the printer.

In the printer's root shell, with the drive mounted at `/mnt/exUDISK`:

```sh
cd /mnt/exUDISK
sha256sum -c cc2camera-hid-armv7-linux.sha256
```

Continue only if the checksum reports `OK`. If `sha256sum` is unavailable,
verify the checksum on your computer first. Adjust the drive path if needed.

```sh
cp cc2camera-hid-armv7-linux /tmp/cc2camera-hid
chmod 755 /tmp/cc2camera-hid
/tmp/cc2camera-hid inspect
```

`inspect` reads the camera identification and queries its version. It does not
upload anything or change camera files. **You can proceed directly to install
without rebooting.** A message saying the version is unavailable is expected
on cameras without a temporary version file; installation performs the firmware
checks. Stop if inspection reports an error or an unsupported camera.

## Install

If you accept the installation risk above, run:

```sh
/tmp/cc2camera-hid install --accept-no-backup-space-risk --verbose > /tmp/cc2camera-install.log 2>&1
cat /tmp/cc2camera-install.log
```

Keep power on until the command finishes. Success begins with:

```text
Camera reports exact installed hook contents and permissions verified.
```

This means the startup scripts were written and checked. They take effect on
the next camera boot. Any error instead requires the steps below, not a retry.

If you know your camera already has custom startup scripts, adding
`--overwrite-managed-scripts` permits replacing `/etc/conf.d/system.sh` and
`/etc/conf.d/enabled/10-erase-fix.sh`. Custom behavior in those two files will be
replaced; other enabled scripts remain. Without the flag, different contents
are refused. A stock camera does not need this flag. If you encounter that
refusal unexpectedly, keep the log and ask for help before another attempt.

## Verify after installation

After the success message, wait at least two minutes, then turn the idle printer
fully off and back on. A shell `reboot` may leave the camera powered and will
not reliably activate the fix. Copy the executable back from the USB drive:

```sh
cp /mnt/exUDISK/cc2camera-hid-armv7-linux /tmp/cc2camera-hid
chmod 755 /tmp/cc2camera-hid
/tmp/cc2camera-hid verify --verbose > /tmp/cc2camera-verify.log 2>&1
cat /tmp/cc2camera-verify.log
```

Verification runs a temporary probe on the camera, without changing its
persistent files or applying the fix itself. Success is:

```text
Camera reports canonical hooks and the live erase correction verified for this boot.
```

Check that the camera feed works too, and include that result in your issue.

## If a command fails

Keep the camera powered and save the log to the USB drive before it is lost
on a printer restart, for example:

```sh
cp /tmp/cc2camera-install.log /mnt/exUDISK/
sync
```

Use `cc2camera-verify.log` instead for a verification failure. Open an issue with
the log and command you ran. `--verbose` captures the HID communication; it can
also be added to `inspect` when diagnosing a connection problem.

Do not repeat an upload or restart simply because a command failed. A preflight
refusal means installation writes did not start; a partial-installation error
means they did. A timeout or USB error leaves the outcome unknown, and a camera
worker may still be running. The uploader also cannot accept another attempt
after launching a worker during the same camera boot. Resolve the failure with
help before deciding whether a power cycle is appropriate.

For protocol details, build instructions and validation evidence, see the
[technical report](report.md).

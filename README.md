# Prevent or recover the Elegoo CC2 stock camera failure

Some stock Elegoo Centauri Carbon 2 cameras can stop working after repeated
printer power cycles. This repository provides two ways to protect or recover
an affected camera:

- a camera that still works can be backed up and a preventive image can be
  built through USB; the current one-command USB write path is not yet ready
  for end users;
- a camera that no longer starts can be recovered from its own flash backup
  with an external programmer.

Both routes preserve the identity of **your** camera. There is no generic
firmware image in this repository, and you must never write another camera's
dump to your device.

> **AI-development note:** This fix was developed with the assistance of AI.
> It has produced the expected result in two tested cases, but if you have any
> doubts, independently audit the tools before using them on your hardware.

## First identify your camera

The known failure affects the `EF-S7-V1.0.30B` camera family. A newer
`EF-S7-V1.0.30D` revision uses different hardware and software and is not
affected by this particular problem.

Do this identification before building a USB cable, buying a programmer, or
running either tool.

1. Power the printer off and unplug it from mains power.
2. Remove the camera module from the printer by undoing its single mounting
   screw, then unplug its four-wire cable.
3. Look at the large processor on the camera mainboard that's now in front of you.

If it is marked `TX5110`, the camera is likely the newer, unaffected family.
This recovery does not apply. If it is marked `Ingenic T23`, or the marking is
unclear, remove the two housing screws and check the complete PCB revision.

![TX5110 processor on a newer CC2 camera](docs/images/ef-s7-v1.0.30d-tx5110.jpg)

| Identification | What is known |
|---|---|
| `EF-S7-V1.0.30B` / Ingenic T23 | This is the family on which the failure has been observed. Continue below. |
| `EF-S7-V1.0.30D` / likely TX5110 | This revision is not affected by the known failure. This guide does not apply. |
| Any other revision or processor | It has not been investigated. We do not know whether it is affected, and this guide does not apply. |

![EF-S7-V1.0.30D PCB marking](docs/images/ef-s7-v1.0.30d-revision.png)

Photographs of the `EF-S7-V1.0.30B` board are available in the
[OpenCentauri camera documentation](https://docs.opencentauri.cc/hardware/CC2/camera/).
The processor marking is a quick screening aid; the full PCB marking is the
authoritative visual identification.

## Choose the path that matches your camera

### The affected camera still works

Use the [USB prevention route](#working-camera-usb-prevention). It needs a
simple camera-to-USB cable but no SPI programmer.

### The affected camera no longer works

First check whether the fault is isolated to the camera:

1. Leave the stock camera disconnected.
2. While the printer is off, connect a known-working ordinary USB webcam to
   the printer's front USB port.
3. Power the printer on.

If the replacement webcam produces a feed, the stock camera is likely the
problem. Continue with the
[hardware recovery route](#failed-camera-hardware-recovery).

If the replacement webcam also fails, this test has not isolated the problem
to the stock camera. Stop this camera-recovery procedure.

> **Temporary replacement:** Once the stock camera is disconnected, most
> ordinary USB webcams can be used in the front USB port while you wait for a
> replacement or decide whether to attempt recovery. Connect the webcam before
> powering the printer on so it has the best chance of becoming `/dev/video0`,
> which is the camera device the printer expects. The likely drawback is an
> inconvenient camera angle unless you improvise or print a mount.

## Working camera: USB prevention

> **USB write currently blocked:** A physical experiment completed a full
> bootloader transfer, full-flash write, reboot, video test, and independent
> three-read verification. It also exposed a failure in the preceding
> normal-mode trigger: on an occupied JFFS2 eraseblock, the stock Linux driver
> can corrupt the upgrade words without entering U-Boot. Do not run
> `cc2flash restore` on an ordinary camera until that entry path is fixed and
> validated as one operation. See the
> [physical validation record](usb-maintenance/PHYSICAL-VALIDATION.md).

This route currently backs up the camera and builds the preventive patch from
that backup without requiring an SPI programmer. The final USB write is under
development because the physical validation found a repeatable trigger failure.

### What you need

- the affected camera, still able to start;
- access to a printer with a 0.2 mm nozzle;
- four P50 pogo pins, such as P50-J1;
- the USB-A plug and cable from an unused USB data cable;
- Python 3.10 or later;
- the Android platform `adb` executable;
- the optional Python `hidapi` package installed with `cc2flash`.

### Build the camera-to-USB cable

The included [printable pogo-pin adapter](docs/models/cc2-camera-p50-pogo-adapter.stl)
acts as a plug for the four-pin socket on the camera PCB. Print it with a
**0.2 mm nozzle** and fit four P50 pogo pins; P50-J1 is one suitable example.

![Printed pogo-pin camera adapter attached to a USB cable](docs/images/cc2-camera-p50-pogo-adapter.jpg)

The computer end can be salvaged from an old USB-A cable. Cut off the unwanted
device end, leaving the USB-A plug and enough cable to work with. Make sure it
is a data cable with all four conductors rather than a charge-only cable.

The camera connector carries ordinary USB 2.0:

| Camera pin | Signal | USB-A pin | Typical USB cable color |
|---:|---|---:|---|
| 1 | GND | 4 | Black |
| 2 | D+ | 3 | Green |
| 3 | D- | 2 | White |
| 4 | +5 V | 1 | Red |

Do not trust wire colors unverified. Before connecting the camera, use a
multimeter to confirm every conductor from the USB plug to its pogo pin and
confirm that +5 V is not shorted to ground or either data line.

### Current safe command sequence

Installation and detailed stop conditions are documented in the
[USB-maintenance reference](usb-maintenance/REFERENCE.md). After `cc2flash`
and `adb` are installed, the currently supported preparation sequence is:

```sh
cc2flash start-adb
cc2flash backup backup.zip
python hardware-recovery/cc2_sig_tool.py build backup.zip
cc2flash plan-restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip
```

On Windows, use `py` instead of `python` if that is how Python was installed.

`start-adb` temporarily starts the camera's existing root ADB service.
`backup` then reads the complete flash repeatedly and publishes `backup.zip`
only after obtaining three consecutive identical images. Keep that ZIP
unchanged and in a separate safe location.

The builder validates the backup and creates the camera-specific preventive
image. `plan-restore` performs the same-camera and allowed-change checks
without opening USB. Stop there and preserve both files. A successful plan does
not make the current `restore` trigger safe.

Stop if any command refuses the camera, backup, firmware, partition layout, or
generated image. Do not work around a validation failure.

## Failed camera: hardware recovery

This route reads and rewrites the camera's eight-pin SPI flash with an external
programmer. The recommended beginner setup uses the NeoProgrammer graphical
application for every flash operation; only the recovery-image build requires
a command line.

### What you need

- a Windows computer;
- a CH341A/CH341B programmer verified for 3.3 V operation;
- an SOIC-8 test clip and cable;
- a multimeter;
- NeoProgrammer;
- Python 3.10 or later;
- the files from this repository.

The CH341 worked with the flash still soldered on the tested camera. A Bus
Pirate is possible but was not reliable in-circuit on that setup; see the
[programmer reference](hardware-recovery/PROGRAMMER_REFERENCE.md) if you need
that alternative.

### 1. Verify voltage before connecting the camera

With the programmer connected to USB but no camera or clip attached, measure
CH341A pin 28 (`VCC`) relative to ground. It should be approximately **3.3 V**.
Also measure the programmer's flash `VCC` output if possible.

If a measured supply or logic level is near 5 V, stop. Board color, seller
description, purchase date, and jumper position are not proof that a
programmer is safe for a 3 V flash chip.

### 2. Connect the flash

Keep the camera's normal USB/power cable disconnected. Connect the SOIC-8 clip
before plugging the programmer into USB. Align the clip's pin-1/red-stripe
conductor with the flash chip's pin-1 dot or notch, and use the programmer's
25-series SPI position.

Confirm the complete marking on the flash itself. The verified 30B family uses
a 64 Mbit/8 MiB `ZB25VQ64`-family 3 V SPI NOR. If the actual chip, voltage,
capacity, or detected profile differs, stop rather than selecting a merely
similar chip.

### 3. Make three trustworthy backups

In NeoProgrammer:

1. Select **Detect IC** and confirm the exact chip profile and 8 MiB capacity.
2. Select **Read IC** and save the full buffer as `cc2-camera-1.bin`.
3. Without moving the clip, read again and save `cc2-camera-2.bin`.
4. Read a third time and save `cc2-camera-3.bin`.

Each file must be exactly 8,388,608 bytes. The recovery builder compares all
three byte-for-byte. All-`00`, all-`FF`, unstable, wrong-size, or inconsistent
reads mean the connection is not trustworthy. Do not erase anything.

Copy all three files to a separate safe location before continuing. They are
the only verified backup of this camera's identity-bearing data.

### 4. Build the recovery image

In Windows Explorer, open the folder containing `cc2_sig_tool.py` and the three
dump files. Click the address bar, type `cmd`, and press Enter. Then run this
single command:

```bat
py cc2_sig_tool.py build cc2-camera-1.bin --confirm cc2-camera-2.bin cc2-camera-3.bin
```

The command performs the stability, firmware, flash-layout, identity, patch,
and generated-image checks. It must finish without an error and create:

```text
cc2-camera-1-cc2-recovery\cc2-camera-recovery.bin
```

If it refuses the dump or reports unfamiliar data, stop. There is deliberately
no general `--force` option.

### 5. Erase, program, and verify

In NeoProgrammer:

1. Detect the chip again and reconfirm its exact profile and 8 MiB capacity.
2. Open the generated `cc2-camera-recovery.bin`. Confirm that the loaded file
   is exactly 8,388,608 bytes. Do not select `config-restored.bin`,
   `serial.cfg`, or any individual region file.
3. Use NeoProgrammer's automatic write sequence with **Erase**,
   **Blank Check**, **Write/Program**, and **Verify** enabled.
4. Require full-chip Verify to finish without any mismatch.

A failed erase, blank check, program, or verify is not a usable result. Keep
the setup connected and diagnose it rather than trying to boot the camera.

Only after complete verification succeeds should you unplug the programmer,
remove the clip, reconnect the camera's normal cable, and test it in the
printer.

## What the repair changes

Affected cameras repeatedly rewrite the same configuration files during every
boot. Old filesystem records accumulate until the writable configuration area
can no longer accept the next boot-time writes. The repair compacts that area,
preserves the camera's own identity, and changes the startup behavior so the
default files are created only when missing.

The builder accepts only the known 30B firmware family and allows changes only
inside the audited startup-script window and writable configuration partition.
Technical details, offsets, supported image fingerprints, config modes, and
limitations are in the
[hardware-recovery technical reference](hardware-recovery/TECHNICAL_DETAILS.md).

## Privacy and backups

Raw dumps, `backup.zip`, `serial.cfg`, and generated recovery images contain
unit-specific identifiers. Do not publish them unredacted. Preserve the
original backup separately from the computer used for recovery.

No full camera dump or vendor firmware image is included in this repository.

## Advanced and audit documentation

- [Hardware-recovery CLI reference](hardware-recovery/CLI_REFERENCE.md)
- [Programmer and alternative-hardware reference](hardware-recovery/PROGRAMMER_REFERENCE.md)
- [Independent recovery-tool verification](hardware-recovery/CC2_RECOVERY_VERIFICATION.md)
- [Hardware-recovery regression results](hardware-recovery/TEST_RESULTS.md)
- [USB-maintenance reference](usb-maintenance/REFERENCE.md)
- [USB project and hardware-validation status](usb-maintenance/PROJECT-STATUS.md)
- [Physical USB restore validation](usb-maintenance/PHYSICAL-VALIDATION.md)
- [Recovered USB protocol](usb-maintenance/PROTOCOL.md)
- [USB reverse-engineering evidence](usb-maintenance/EVIDENCE.md)


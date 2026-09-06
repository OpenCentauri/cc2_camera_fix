# Programmer and electrical reference

The recommended NeoProgrammer workflow is documented in the [main user guide](../README.md#failed-camera-hardware-recovery).
This reference retains the complete electrical mapping and alternative-programmer notes.

## Programmer instructions

### Recommended: NeoProgrammer with CH341A/CH341B

NeoProgrammer does not use `cc2-camera-layout.txt`. This workflow erases, writes, and verifies the **complete 8 MiB** `cc2-camera-recovery.bin`, including the unit-specific data copied from the validated input dump.

#### Electrical checks

1. Read the complete part marking on the camera's SPI flash. The verified firmware family uses a 64 Mbit/8 MiB `ZB25VQ64`-family 3 V SPI NOR, but the exact chip and voltage on the board in front of you take precedence. The manufacturer describes `ZB25VQ64` as a 3 V device; do not infer voltage from programmer color, seller description, or a jumper alone. See the [ZB25VQ64 product information](https://www.gdzhianxin.com/index.php?a=show&c=index&catid=108&id=91&m=content).
2. With no camera or clip attached, perform the pin-28 voltage check described above. Also measure the programmer's flash `VCC` output and, if possible, its SPI logic-high levels. Use a proper level/voltage adapter if any measured level does not match the exact flash datasheet.
3. Disconnect the camera's USB cable and every other source of board power. Do not power the camera normally while the programmer supplies the flash. If your successful read setup uses a different, independently verified power arrangement, reproduce that exact arrangement; never connect two power sources blindly.
4. Connect the clip before plugging the CH341 programmer into USB. Align the clip's pin-1/red-stripe conductor with the flash's pin-1 dot/notch, and place the adapter in the programmer's **25-series SPI** position, not the 24-series I²C position.

Standard SOIC-8 SPI NOR signals are:

| Flash pin | Signal | CH341 label |
|---:|---|---|
| 1 | `CS#` | `CS` |
| 2 | `IO1/DO` | `MISO` |
| 3 | `IO2/WP#` | `WP` or pulled high |
| 4 | `GND` | `GND` |
| 5 | `IO0/DI` | `MOSI` |
| 6 | `CLK` | `CLK` |
| 7 | `IO3/HOLD#/RESET#` | `HOLD` or pulled high |
| 8 | `VCC` | verified 3 V supply |

Confirm this mapping against the exact flash datasheet and adapter silkscreen. Do not rely on the table if the package or adapter differs.

#### Establish a trustworthy backup

1. Install the appropriate CH341 driver and start NeoProgrammer. Obtain executables/drivers from a source you trust and scan them before use.
2. Use **Detect IC**, then confirm that the reported manufacturer/device ID, capacity, and exact selected profile correspond to the chip marking. Select `ZB25VQ64` only when that is the actual part. If the exact chip is unavailable, detection is inconsistent, or NeoProgrammer proposes only a vaguely similar `25Q64`, stop rather than guessing.
3. Click **Read IC**, wait for the complete 8 MiB buffer, and save it as `cc2-camera-1.bin`.
4. Without moving the clip, repeat the read twice and save `cc2-camera-2.bin` and `cc2-camera-3.bin`.
5. Require all three files to be exactly 8,388,608 bytes and have identical SHA-256 hashes. On Windows, for example:

```bat
certutil -hashfile cc2-camera-1.bin SHA256
certutil -hashfile cc2-camera-2.bin SHA256
certutil -hashfile cc2-camera-3.bin SHA256
```

All-`00`, all-`FF`, unstable, differently hashed, wrong-size, or intermittently detected reads mean the clip/power/in-circuit setup is not trustworthy. Stop and correct it before any erase or write.

Build the recovery image from those reads:

```bat
cc2flash build-image cc2-camera-1.bin --confirm-read cc2-camera-2.bin --confirm-read cc2-camera-3.bin
```

#### Full-chip erase, program, and verify

1. In NeoProgrammer, re-detect and re-confirm the exact chip profile and 8 MiB capacity.
2. Open the generated `cc2-camera-recovery.bin`. Confirm the loaded buffer/file is exactly 8,388,608 bytes. Do **not** load `config-restored.bin`, `serial.cfg`, or an individual layout region as the full-chip image.
3. Save the three original dumps somewhere separate before continuing. They contain the only verified copy of this camera's unit identity.
4. Use NeoProgrammer's automatic write sequence with **Erase**, **Blank Check**, **Write/Program**, and **Verify** enabled. Depending on the version, this is exposed through **Write IC** or its adjacent operation menu.
5. Do not edit status registers, OTP/security areas, unique IDs, or protection bits pre-emptively. If NeoProgrammer reports write protection, stop and identify the exact status-register meaning from the selected chip's datasheet before changing it.
6. Require NeoProgrammer's verify operation to complete without any mismatch. A failed erase, blank check, write, or verify is not a usable result; keep the setup connected and diagnose it rather than trying to boot.
7. Without moving the clip, run **Read IC** again and save the complete result as `cc2-camera-readback.bin`.
8. Verify that readback with the recovery tool:

```bat
fc.exe /b cc2-camera-1-cc2-recovery\cc2-camera-recovery.bin cc2-camera-readback.bin
```

Only after the tool reports a byte-for-byte match should you unplug the CH341 programmer, remove the clip, reconnect normal camera power, and test boot/USB enumeration.

NeoProgrammer's labels can vary slightly by release, but the required operation order does not: **three stable reads → build → full-chip erase → blank check → program → internal verify → full readback → tool verify**. NeoProgrammer describes the same backup/erase/write/verify capabilities in its [software overview](https://neoprogrammer.org/).

### Alternative: flashrom with a Bus Pirate

The generated `FLASHING.txt` also contains a `flashrom` command for the Bus Pirate. Unlike the NeoProgrammer workflow, it writes only the regions reported as changed and deliberately does not enable programmer-supplied target power.

In-circuit Bus Pirate access may not work on this camera because the rest of the board can interfere with the SPI bus. In the tested setup, the CH341 worked with the flash still soldered to the board, while the Bus Pirate worked only after the chip was unsoldered. Do not interpret unstable or failed reads as permission to write: obtain three identical full dumps first, or switch to the CH341 approach.

If you do use a Bus Pirate, independently verify the exact flash voltage, pinout, wiring, and power arrangement. Never power the camera normally while the programmer is connected unless you have explicitly designed and verified that arrangement.

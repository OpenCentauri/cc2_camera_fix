# Raspberry Pi 3 SPI programmer

The Raspberry Pi 3 can be used as an alternative SPI programmer for the CC2 camera flash using an SOIC-8 clip.

## Hardware wiring

| Flash pin | Signal | Raspberry Pi physical pin | Pi signal |
|---:|---|---:|---|
| 1 | CS | 24 | GPIO8 / CE0 |
| 2 | DO (MISO) | 21 | GPIO9 / MISO |
| 3 | WP | 17 | 3.3 V |
| 4 | GND | 25 | GND |
| 5 | DI (MOSI) | 19 | GPIO10 / MOSI |
| 6 | CLK | 23 | GPIO11 / SCLK |
| 7 | HOLD | 17 | 3.3 V |
| 8 | VCC | 1 | 3.3 V |

WP and HOLD share the same 3.3 V connection.

**Important:**
- Verify the flash chip's voltage requirements before connecting it.
- Never apply 5 V to the flash or Raspberry Pi GPIO.
- Disconnect normal camera/printer power while using the Raspberry Pi programmer.
- Check the SOIC-8 pin-1 orientation before connecting the clip.

## Enable SPI

Enable the SPI interface:

```bash
sudo raspi-config nonint do_spi 0
sudo reboot
````

## Install flashrom
```bash
sudo apt update
sudo apt install flashrom
```

## Detect the flash chip

```bash
sudo flashrom \
  -p linux_spi:dev=/dev/spidev0.0,spispeed=1000
```

The tested setup detected an unknown SFDP-capable SPI flash with a capacity of 8192 kB (8 MiB).

Native flashrom chip support is not required for SFDP-based read operations, but write and erase compatibility must be established separately.

## Create three independent backups

Run three consecutive reads without moving the SOIC-8 clip:
```bash
sudo flashrom -p linux_spi:dev=/dev/spidev0.0,spispeed=1000 \
  -r cc2-camera-1.bin

sudo flashrom -p linux_spi:dev=/dev/spidev0.0,spispeed=1000 \
  -r cc2-camera-2.bin

sudo flashrom -p linux_spi:dev=/dev/spidev0.0,spispeed=1000 \
  -r cc2-camera-3.bin
```

Check the file sizes:
```bash
stat -c '%n %s bytes' cc2-camera-*.bin
```

Each backup should be exactly 8388608 bytes.

Compare SHA-256 hashes:
```bash
sha256sum cc2-camera-1.bin \
          cc2-camera-2.bin \
          cc2-camera-3.bin
```

All three hashes must match.

Additionally, compare the files byte-for-byte:
```bash
cmp cc2-camera-1.bin cc2-camera-2.bin
cmp cc2-camera-1.bin cc2-camera-3.bin
```

No output indicates identical files.

Keep the original backups in a safe location.

## Build the recovery image

Follow the main project instructions to install `cc2camera`.

Run:
```bash
cc2camera build-image cc2-camera-1.bin \
  --confirm-read cc2-camera-2.bin \
  --confirm-read cc2-camera-3.bin
```

The recovery builder must report:
```text
RECOVERY BUILD PASSED
```

Do not bypass validation failures.

## Program the recovery image

Enter the generated recovery directory:
```bash
cd cc2-camera-1-cc2-recovery
```

Program the selected recovery regions:
```bash
sudo flashrom \
  -p linux_spi:dev=/dev/spidev0.0,spispeed=1000 \
  -l cc2-camera-layout.txt \
  -i system_bashrc_patch \
  -i config \
  -w cc2-camera-recovery.bin
```

Check the complete flashrom output for programming and verification errors.

Warning: This Raspberry Pi write procedure was successfully tested on one camera with an SFDP-detected 8 MiB flash. It is not validated for all flash-chip variants. The project's documented external programmer remains the recommended method.

## Verify the complete flash

Read the entire flash after programming:
```bash
sudo flashrom \
  -p linux_spi:dev=/dev/spidev0.0,spispeed=1000 \
  -r cc2-camera-readback.bin
```

Compare the generated image with the physical flash readback:
```bash
cmp cc2-camera-recovery.bin cc2-camera-readback.bin
```

No output means the files are byte-for-byte identical.

Do not return the camera to service if programming or readback verification fails.

## Tested configuration

- Raspberry Pi 3
- Raspberry Pi OS 64-bit
- Linux 6.18.50+rpt-rpi-v8 (aarch64)
- flashrom 1.4.0
- Linux SPI0
- SPI speed: 1 MHz
- SOIC-8 clip
- 3.3 V flash connection
- 8 MiB SPI NOR flash

## Tested result

- Three consecutive flash reads produced identical SHA-256 hashes.
- OpenCentauri recovery builder passed strict validation.
- Recovery image was programmed using flashrom.
- Full-chip readback matched the generated recovery image.
- Camera successfully resumed operation.

This procedure has been tested on one Raspberry Pi 3 and one camera. Other Raspberry Pi models and flash variants may behave differently.

## Privacy

Do not publish original camera dumps, generated recovery images, `serial.cfg`, or other files containing camera-specific identifiers without removing sensitive information.

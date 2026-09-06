# Hardware-recovery CLI reference

Install cc2flash using the [setup guide](../docs/INSTALLATION.md).
The [main guide](../README.md#failed-camera-hardware-recovery) describes
connection, three independent programmer reads, full-chip programming and
verification. All commands and options are listed in the
[unified CLI reference](../docs/CLI.md).

## Build from programmer reads

```sh
cc2flash inspect-image camera-1.bin --confirm-read camera-2.bin --confirm-read camera-3.bin
cc2flash build-image camera-1.bin --confirm-read camera-2.bin --confirm-read camera-3.bin
```

## Build from a USB backup

```sh
cc2flash inspect-image backup.zip
cc2flash build-image backup.zip
cc2flash restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip --dry-run
```

Keep the backup ZIP unchanged. It contains both flash.bin and acquisition
evidence and is required for a real USB restore. A programmer can write the
generated full image using the procedure in the main guide.

## Config choices

The default `--config-mode serial-only` rebuilds config containing only
serial.cfg. Missing defaults are created on the next boot.
`--config-mode preserve-files` rebuilds every supported live regular file
once, retaining contents and metadata but dropping obsolete records.
`--wipe-unknown-config` explicitly discards unfamiliar config names in
serial-only mode without weakening identity validation.

The original image is never modified. By default the new bundle is written
beside it in `<input-stem>-cc2-recovery/`:

```text
cc2-camera-recovery.bin
cc2-camera-layout.txt
config-restored.bin
serial.cfg
MANIFEST.json
VALIDATION.txt
FLASHING.txt
SHA256SUMS.txt
```

Use the complete cc2-camera-recovery.bin for full-chip programming, not
config-restored.bin or serial.cfg. FLASHING.txt also contains an advanced
flashrom region-write template based on regions that actually changed.
Regardless of write method, require full-chip verification against the
generated image before disconnecting the programmer and booting the camera.

For a separate saved readback, use `fc.exe /b expected.bin readback.bin`
on Windows or `cmp expected.bin readback.bin` on Linux/macOS.
Never connect programmer target power and normal camera power simultaneously.

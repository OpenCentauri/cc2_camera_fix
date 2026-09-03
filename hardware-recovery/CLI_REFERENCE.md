# Hardware-recovery CLI reference

The normal recovery path is documented in the [main user guide](../README.md#failed-camera-hardware-recovery).
This reference contains optional, diagnostic, and advanced commands.

## Usage

Download `cc2_sig_tool.py` from this repository and place it in a working directory. You need Python 3.10 or later and either a raw full 8 MiB dump from your camera or an unmodified `cc2flash-backup-v2` ZIP created by the USB-maintenance tool. Do not continue to a write until you have evidence for three identical physical reads.

For raw programmer dumps, supply three independently read files with identical
SHA-256 hashes as shown below. For a USB backup ZIP, the builder validates
`flash.bin`, `manifest.json`, the exact member set, hashes, boot fingerprint,
partition map, and three-consecutive-read acquisition evidence. A valid ZIP
therefore satisfies the three-read gate directly; do not extract or rewrite it.

The command-line examples in this section use a normal Unix shell and
`python3`. The later NeoProgrammer walkthrough uses Windows syntax where the
programmer software requires it.

Run the internal self-test. Supplying a supported ROM also exercises the complete readable SquashFS patch path:

```sh
python3 cc2_sig_tool.py self-test cc2-camera-1.bin
```

Analyze without creating anything:

```sh
python3 cc2_sig_tool.py analyze cc2-camera-1.bin
```

The equivalent USB-backup workflow is:

```sh
python3 cc2_sig_tool.py analyze backup.zip
python3 cc2_sig_tool.py build backup.zip
```

The second command creates
`backup-cc2-recovery/cc2-camera-recovery.bin`. Keep the original
`backup.zip` unchanged: USB maintenance uses it both as the preserved
three-read backup and to prove that the recovery image belongs to the same
camera before restoring it.

Choose the config treatment explicitly when the default is not appropriate:

```sh
# Default: wipe ordinary config and preserve only serial.cfg
python3 cc2_sig_tool.py build backup.zip --config-mode clean-data

# Same clean rebuild, after explicitly approving unfamiliar config names
python3 cc2_sig_tool.py build backup.zip --wipe-unknown-config

# Recreate every live regular config file once, without dead JFFS2 copies
python3 cc2_sig_tool.py build backup.zip --config-mode preserve-data
```

Raw 8 MiB backups are first-class inputs. For a bricked camera, raw dumps made
with an external programmer are the only acquisition path; supply three stable
reads with `--confirm` as described below.

Build a recovery bundle and require three physical reads to be byte-identical:

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --confirm cc2-camera-2.bin cc2-camera-3.bin
```

For a non-reference unit, three byte-identical physical reads are required by default. With fewer reads, the tool refuses unless the higher risk is explicitly accepted:

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --allow-fewer-reads
```

Exact full reference dumps listed in `REFERENCE_FINGERPRINTS.json` may be rebuilt from one copy because their complete 8 MiB hashes already match known inputs.

To validate the complete USB round trip without opening USB:

```sh
cc2flash plan-restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip
```

If that passes, the corresponding guarded write command is:

```sh
cc2flash restore backup-cc2-recovery/cc2-camera-recovery.bin --backup backup.zip
```

USB maintenance refuses the candidate if any byte outside this builder's
audited `0x463000–0x46AFFF` system window and
`0x7E0000–0x7FFFFF` config partition differs from the preserved backup.

For the raw `cc2-camera-1.bin` example, the default output directory is:

```text
cc2-camera-1-cc2-recovery
```

It contains:

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

`FLASHING.txt` contains a command assembled from the regions that actually changed. For example:

- exhausted stock image: `system_bashrc_patch` and `config`;
- already config-recovered image: normally `system_bashrc_patch` only;
- already permanently patched image: no write required.

To patch only `bashrc.sh` while preserving the current config partition byte-for-byte:

```sh
python3 cc2_sig_tool.py build cc2-camera-1.bin --keep-config
```

This option is accepted only when the partition already equals the exact canonical rebuild. It refuses exhausted or otherwise noncanonical config, because preserving one could leave the device bricked.
It cannot be combined with `--config-mode preserve-data` or
`--wipe-unknown-config`.

## Readback verification

After writing, make a full 8 MiB readback without disturbing the programmer connection. Then run:

```bat
py cc2_sig_tool.py verify cc2-camera-recovery.bin cc2-camera-readback.bin
```

Success is reported only when the files are byte-for-byte identical.

`verify` first requires the expected file to be a strict, patched recovery image
with a safely reconstructable config, then requires the full readback to be
byte-for-byte identical. Both clean-data and preserve-data outputs are valid.

Only disconnect the programmer and attempt a normal boot after this comparison succeeds. Never connect normal camera/USB power and programmer target power simultaneously.

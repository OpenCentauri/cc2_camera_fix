# Static-analysis and runtime evidence

## Inputs

| Artifact | Size | SHA-256 |
|---|---:|---|
| supplied SPI dump | 8,388,608 | `269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f` |
| recovered `/bin/hid_update` | 19,048 | `bcbdd698b4b9208f73f7ef1f9846b3a96130a1bcc5c118c0e71543cf4e1ed503` |

Recovered `/bin/hid_update` MD5 is
`8091751fdd4d0d50ea31901663797a86`, matching the task's known binary.

## Flash mapping

Magic and filesystem parsing confirm:

| Flash range | Content |
|---|---|
| `0x000000–0x03ffff` | SPL and U-Boot |
| `0x040000–0x18ffff` | uImage Linux kernel |
| `0x190000–0x2e7fff` | SquashFS root |
| `0x2e8000–0x7cffff` | SquashFS system |
| `0x7d0000–0x7dffff` | plaintext HWCONFIG partition |
| `0x7e0000–0x7fffff` | JFFS2 config partition |

The dependency-free extractor in `tools/squashfs_extract.py` handles the two
XZ-compressed SquashFS 4 filesystems in this dump.  Extracted vendor files and
the supplied dump are not redistributed with this project.

## Linux updater anchors

The recovered MIPS32/uClibc ELF opens `/dev/hidg0` and `/dev/ucamera`.
Disassembly confirms:

| Function | Finding |
|---|---|
| response builder at `0x00401034` | fixed 1024-byte report and normal CRC |
| parser/dispatcher at `0x0040123c` | magic, CRC, status, command groups |
| configuration GET at `0x00401cd0` | reads one of 21 keyed values |
| configuration SET at `0x00402038` | tail-buffered keyed rewrite and zero-padding |
| configuration dispatcher at `0x004024c0` | all 42 even/odd GET/SET commands |
| upload initialization at `0x00402830` | allocates 16 MiB + 512-byte receive buffer |
| upload packet append at `0x004028dc` | sequence/length/final-packet handling |
| upload commit at `0x004029f0` | shell remove, file write, chmod, optional daemon backup |
| upload dispatcher at `0x00402e3c` | all 13 recognized `0x3xxx` commands and five states |
| high-nibble `0x4000` branch | write/read eight bytes at flash `0x7f8000`, reply, reboot |

The transition ioctls are `0x2000550c` (write) and `0x2000550d` (read), with a
driver request length of eight bytes.

## SPL anchors

The SPL is in the first flash boot region and its analyzed code maps directly
from flash offset zero to the linked `0xf0000000` region. At linked address
`0xf0001c28` it reads eight bytes from SPI offset `0x7f8000`. At
`0xf0001c54` it constructs and compares `0x55504454`.

## Main U-Boot mapping

The main U-Boot binary starts at flash offset `0x6800`, linked at
`0x80100000`.  Equivalently, a flash file offset maps to virtual address:

```text
VA = 0x800f9800 + flash_offset
```

Its canonical PIC global pointer is `0x80136ef0`.

| Address / file offset | Finding |
|---|---|
| file `0x354bc` | HID USB descriptor, `a108:ff08` |
| file `0x35607` | HID report descriptor, ID 1 and 3071-byte reports |
| `0x80117948` | host-frame magic and additive-checksum validator |
| `0x801179ec` | device-frame builder (`81 00 ee`) |
| `0x80117d1c` | complete metadata/data receive state machine |
| `0x801186ac` | SPI flash initialization |
| `0x80118714` | read flags and select CDC/HID transport |
| `0x8011b9d8` | HID response envelope; fixed 3072-byte USB request |
| `0x80119620` | MD5 implementation |

Within `0x80117d1c`, the only accepted host types are 1 and 3.  The final path
copies the first 128 bytes to a local header, computes MD5 over the following
`header.image_size` bytes, compares header bytes 12–27, and invokes the SPI
object's erase and write methods using header words at offsets 4 and 8.

The metadata branch calls the host-frame validator at `0x80117948` but does not
branch on its return value before copying ten payload bytes. The data branch at
`0x801181d8` does test the validator result. This establishes that metadata magic
and type are checked by the outer branch while metadata length/checksum are not
fully enforced; type-3 checksums are enforced.

## ADB startup and backup path

The extracted root filesystem contains `/bin/adbd` and BusyBox. Both active
system camera configurations contain `adb_en :1`, and strings in `adbd` include
its root-service paths. The default image does not directly launch `adbd`.

The root SquashFS `/etc/init.d/rcS` does, however, contain a generic persistent
startup hook. Its established boot order is:

1. identify the partition named `config`;
2. mount its JFFS2 filesystem at `/etc/conf.d`;
3. test for `/etc/conf.d/system.sh` and execute it when present; and
4. only then mount the system SquashFS and run `/home/bashrc.sh`.

Because `/bin/adbd` is in root SquashFS, the exact file contents `/bin/adbd &`
are valid at that point. The device owner independently runtime-verified that
manually creating this file starts ADB after restart. The normal-HID uploader's
ability to select an arbitrary persistent path, append the 11 content bytes,
remove/recreate the target, and chmod it `0777` is statically confirmed at the
Linux updater anchors above. The persistent-file client HID sequence remains
runtime-unverified in this analysis. The separate temporary command-injection
sequence has since been verified on physical hardware as described below.

## Live Windows ADB transport validation

The device owner ran the client-generated temporary startup sequence against a
physical camera on Windows 11 with Google ADB 35.0.2. The sequence started the
stock daemon, changed `Ucamera001` from `offline` to `device`, and provided a
root interactive shell. The old composite transport first returned
`error: closed`, establishing the need for bounded state polling after HID.

The same session compared the daemon's read services:

| Probe | Result |
|---|---|
| `adb shell id` | success: `uid=0(root) gid=0(root)` |
| `adb exec-out id` | failure: `error: closed` |
| `adb shell cat /proc/mtd` | success; expected six-partition map, with doubled CR on Windows |
| `adb pull /proc/mtd` | success: 227 bytes |
| camera `md5sum /dev/mtd4` | `56392b3d32797a089432c7b633ef921f` |
| `adb pull /dev/mtd4` | success: 65,536 bytes; local MD5 matched camera |

The pulled `mtd4` SHA-256 was
`ff1d60111d2517e1423a0b9c72b7b3c62e9388e54217929cbd33e2d16909958c`.
This establishes ADB sync/`pull` as a byte-preserving Windows transport for a
raw MTD character device on this firmware. It does not yet establish a complete
six-partition, two-pass acquisition; the client retains exact per-partition
sizes and whole-image equality as mandatory gates for that test.

## Corrected bootloader ACK interpretation

The type-2 frame builder receives four bytes from U-Boot state at
`global + 0x6118`. Metadata initialization clears that value, producing the
initial ACK `00 00 00 00`. After a successful nonfinal batch, the state machine
adds `packets_per_ack` to the current absolute packet base, stores the result at
the same location, and transmits it. Thus the type-2 payload is
`u32le(next_expected_packet)`, not four constant zero bytes. The existing
v0.1.0 Python transport's data-ACK comparison is not hardware-compatible.

## Human-readable reconstruction

`source-reconstruction/` contains annotated C-like source for the Linux daemon,
the SPL upgrade gate, and the U-Boot updater. It is explicitly not presented as
the vendor's original source: recovered constants, branches, paths, packet
fields, and state transitions are separated from inferred names and normalized
control flow. `source-reconstruction/TRACEABILITY.md` maps each reconstructed
function to its original virtual address, while `REPRODUCING.md` gives read-only
commands for regenerating the relevant disassembly from the supplied image.

# SFC preparation for stock HID restore

## Scope and prerequisites

`restore` temporarily changes one internal Linux SFC erase-size word before
asking the stock HID updater to write its bootloader flag. It does not install
an updater, replace bootloader software, erase config manually, or stop
`ucamera`. The subsequent stock bootloader transfer still replaces the full
8 MiB flash with the explicitly confirmed, same-camera recovery image.

Use an independently preserved three-consecutive-read backup. Keep dumps outside
the repository. The recovery image must match it outside the audited system
patch window and config partition. Start root ADB explicitly before restore:

```sh
cc2flash start-adb --serial <camera-serial>
cc2flash restore fixed.bin --backup backup.zip --serial <camera-serial>
```

The preparation requires exactly one online ADB device and one normal camera
HID interface, with matching serials. The serial is a transport-selection check,
not proof of unit identity: stable live flash is also compared with the backup,
including HWCONFIG. Unknown kernels, layouts, instructions, pointers, values,
ambiguous devices, and failed checks are refused without a HID upgrade request.
Read-only commands do not perform this preparation.

## Exact supported kernel and pointer derivation

The full kernel partition at `0x040000–0x18ffff` must have SHA-256:

```text
0855c3a93f571c3130f1bdd38469e7c806ce713550a3ce536c81167579341545
```

This gate is checked on the local backup before hardware access and again on
the stable live image. The known kernel uImage loads at `0x80010000` and contains
an LZMA-compressed payload. The following is independently decoded MIPS
interoperability information, not vendor source:

| Virtual instruction address | Instruction | Meaning |
|---|---|---|
| `0x801f06d4` | `lui s3, 0x8044` | Upper part of global address |
| `0x801f06e0` | `lw a0, -20080(s3)` | Load object pointer from `0x8043b190` |
| `0x801f0764` | `lw v0, 16(v0)` | Recovery erase-loop stride at object `+0x10` |
| `0x801efca4` | `lw v0, 16(s1)` | Sector opcode selection from the same member |

The driver selects sector-erase opcode `0x20` for `0x1000`, `0x52` for `0x8000`,
and `0xd8` for `0x10000`. Its `0x4000` configuration has no corresponding branch.

Live `/proc/kallsyms` must contain exactly one entry for each:

| Symbol | Virtual address |
|---|---|
| `recovery_norflash_erase` | `0x801f06cc` |
| `jz_spi_norflash_erase_sector` | `0x801efc74` |
| `direct_erase_norflash` | `0x801f080c` |

The implementation also checks the four live instruction words above. These
fixed code/global addresses apply only to the exact gated kernel, not other
builds. The **object address is read afresh on every invocation**:

```text
object_virtual = read32(0x0043b190)
erase_size_physical = object_virtual - 0x80000000 + 0x10
```

The observed `0x818b4800` pointer yielded `0x018b4810`. That heap address is not
hard-coded. The pointer must be aligned and in the bounded cached 64 MiB RAM
window above kernel/static memory; the inspected object member range must fit.
The MTD partition map must retain its known `0x4000` geometry, and the internal
member must be either `0x4000` or an already-patched `0x1000`.

## Mutation and failure behavior

After all preflight reads, preparation runs `sync`, checks device selection
again, and rechecks the object pointer and old member value inside the same
remote shell invocation as the write. It writes only `0x1000`, reads it back,
requires an explicit success marker, and independently rereads the pointer and
member before permitting the HID trigger. Already-patched state is verified
without rewriting it. Legacy ADB shell exit status alone is not considered
evidence of remote success. Each ADB subprocess retains its bounded timeout.

No rollback to `0x4000` is attempted. A failure after the write may leave the
live field patched; the error says so, and no automatic retry or HID trigger
follows. A later HID timeout may mean a request is pending or the camera has
already rebooted: inspect device state before deciding what to do next. Do not
disconnect power during a bootloader erase/write. Reboot reinitializes the
driver, so this is not a persistent kernel fix.

The stock flag operation requests eight bytes at full-flash `0x7f8000`
(`/dev/mtd5` offset `0x18000`). The decoded loop and opcode selection predict a
single 4 KiB erase, followed by the flag program. **Its exact physical footprint
was not independently measured before the subsequent full-flash restore.**
The flag sector belongs to mounted JFFS2 and can contain live records; successful
upgrade entry does not promise config preservation if the subsequent restore
fails. The preserved backup remains essential.

## Physical evidence versus automated validation

The developer's supplied `0x1000restore.log` and host transcript establish this
manual sequence on one tested camera:

1. Start temporary root ADB through the existing normal-HID command.
2. Sync and manually set/read back the live field as `0x1000`.
3. Run ordinary `cc2flash restore`, leaving `ucamera` running and not manually
   erasing config.
4. Complete all 2,742 packets, return to normal USB, and obtain three consecutive
   identical post-write reads matching every boot-stable byte through HWCONFIG.

Sanitized UART observations:

```text
erase_flash addr=8355840,len=8
write_flash offset=8355840,size=8
Upgrade_flag0 = 0x55504454, Upgrade_flag1 = 0x10203a1
flash_flag1 = 0x55504454, flash_flag_2 = 0x010203a1
Upgrade mode = hid mode!!!
```

The flag operation did not emit the prior erase error. However, the subsequent
Linux boot again emitted `transfer length is error,803,jz_spi_norflash_erase_sector`,
and the developer read back `0x4000`. The startup-script mitigation does not
permanently repair this kernel erase path. Config was deliberately not claimed
byte-exact after the verification boot.

Offline synthetic tests cover preparation success and refusal paths, dynamic
heap addresses, matching transport selection, kernel and instruction gates,
stable-read failures, pointer bounds, unexpected values, sync failure, guarded
write failure, readback failure, and absence of a HID trigger on refusal. Existing
post-restore readback tests remain in place. The supplied kernel was independently
decoded locally; neither it nor the raw log is committed.

**Hardware-unverified:** the integrated automatic preparation, other firmware
revisions, and precise pre-transfer collateral erase footprint. The successful
manual sequence is evidence for feasibility, not a substitute for testing the
new automation on hardware.

# Physical USB restore validation

**Initial experiment date:** 2026-09-04  
**Integrated validation date:** 2026-09-06  
**Hardware:** one `EF-S7-V1.0.30B` camera with an Ingenic T23N and ZB25VQ64

This document records the initial destructive investigation and the later
successful physical validation of the integrated USB restore path. It separates
direct observations from remaining limits. Console excerpts have been shortened and all host paths, camera
serials, unit-specific identifiers, and unit-specific image hashes have been
removed.

## Result at a glance

| Question | Result |
|---|---|
| Can the U-Boot HID updater receive a complete image? | **Yes**, on the tested camera: all 2,742 packets were accepted and the RAM-image MD5 matched. |
| Can it erase and write the full 8 MiB flash? | **Yes**, on the tested camera: normal USB returned and an independent three-read ADB backup matched the candidate through HWCONFIG. |
| Did the camera work afterward? | **Yes**: normal HID returned and a live video picture was observed. |
| Does the integrated `cc2flash restore` enter U-Boot reliably? | **Yes**, on the tested supported camera: it dynamically located and corrected the live SFC field, then the stock flag write entered U-Boot without manual preconditioning. |
| Did the complete integrated restore pass physical verification? | **Yes**: one uninterrupted CLI invocation completed pre-write stable reads, flag creation, bootloader transfer/write, normal reboot, and three-read post-write verification. |

The initial test separated a working bootloader transfer from a broken stock
Linux trigger. The implemented restore path now validates the exact supported
kernel and live camera, dynamically prepares the SFC field, and performs that
transition successfully as part of the complete verified operation.

## Relevant flash layout

The 128 KiB JFFS2 config partition is `/dev/mtd5`, at full-flash range
`0x7e0000–0x7fffff`. The Linux daemon stores the two persistent upgrade words
at full-flash offset `0x7f8000`, which is config-relative offset `0x18000` and
the start of the seventh 16 KiB JFFS2 eraseblock.

The requested eight bytes are:

```text
54 44 50 55 a1 03 02 01
```

They decode as `0x55504454` (`UPDT`) and `0x010203a1` (HID transport). SPL
enters the updater only when it reads those values.

## First attempt: trigger corruption instead of upgrade mode

Before the first restore attempt, the target contained a valid JFFS2
cleanmarker:

```text
85 19 03 20 0c 00 00 00 b1 b0 1e e4
```

The normal-mode HID handler attempted to program the upgrade words without
successfully erasing the containing NOR sector. SPI NOR page programming can
change a bit from 1 to 0, but cannot change 0 back to 1. The physical result
was exactly the bitwise AND of the old and requested bytes:

```text
old:       85 19 03 20 0c 00 00 00
requested: 54 44 50 55 a1 03 02 01
result:    04 00 00 00 00 00 00 00
```

The client consequently timed out waiting for bootloader HID:

```text
Entering bootloader HID mode; the 8-byte flag write begins now.
cc2flash: error: timed out waiting for HID a108:ff08
```

UART proved this was not merely a Windows enumeration race. SPL read the
damaged value and continued into ordinary Linux:

```text
flash_flag1 = 0x00000004, flash_flag_2 = 0x00000000
```

No U-Boot metadata or image packet was sent during this attempt, and no
full-flash erase/write began.

On the following Linux boot, JFFS2 also detected the damaged cleanmarker and
tried to erase the affected block. That erase failed:

```text
jffs2: Magic bitmask 0x1985 not found at 0x00018000: 0x0004 instead
the transfer length is error,803,jz_spi_norflash_erase_sector
jffs2: Newly-erased block contained word 0x4 at offset 0x00018000
```

An independently acquired stable backup confirmed that boot through HWCONFIG
was byte-identical to the pre-attempt backup. Changes were confined to config,
including the exact AND result at `0x7f8000`.

## Kernel erase-size mismatch

The running MTD interface reports a 16 KiB erase size:

```text
/dev/mtd5 size:      131072
/dev/mtd5 erasesize: 16384
```

Static disassembly of the exact camera kernel found that
`jz_spi_norflash_erase_sector` selects opcodes only for these internal sizes:

| Internal size | Opcode | Operation |
|---:|---:|---|
| `0x1000` | `0x20` | 4 KiB sector erase |
| `0x8000` | `0x52` | 32 KiB block erase |
| `0x10000` | `0xd8` | 64 KiB block erase |

There is no branch for the configured `0x4000` value. The erase command is
therefore invalid for this 16 KiB configuration, matching the runtime driver
errors above.

For one test boot, `/proc/kallsyms` and the exported driver object were used to
locate the live internal erase-size field. Its physical address was derived at
runtime; it is **not a stable address across boots or devices**. Changing only
that live field from `0x4000` to `0x1000` caused each 16 KiB MTD erase request
to be split into supported 4 KiB sector erases. `flash_eraseall /dev/mtd5`
then completed without driver errors, and all 128 KiB read back as `0xff`.

During that diagnostic experiment, the field was restored to `0x4000`
afterward. The later implementation derives and validates the live pointer,
sets the field only for the restore transition, and relies on reboot to
reinitialize the driver; it remains a temporary preparation rather than a
persistent kernel fix.

## Lab path that reached U-Boot

The experiment then established the following facts:

1. With the live erase-size field temporarily set to `0x1000`, the config
   partition could be erased and formatted as JFFS2.
2. A camera-specific `serial.cfg` was recreated before reboot. The subsequent
   boot mounted config cleanly and the patched startup script recreated the
   ordinary default files. The optional persistent `system.sh` was absent.
3. Normal attempts to remount config read-only failed because `ucamera` kept
   writable descriptors open. BusyBox `mount -f` only faked the operation, and
   SysRq `u` did not remount this JFFS2 mount.
4. Sending `SIGSTOP` to `ucamera` kept its PID visible to `monitor.sh`, so the
   shell watchdog did not reboot the camera. Normal HID and the temporarily
   started ADB service remained reachable.
5. Erasing config while it was still mounted read-write was physically possible
   with `ucamera` stopped, but is filesystem-unsafe and is not an end-user
   procedure.
6. A restore was triggered while `ucamera` was still stopped, so the host timed
   out waiting for a HID response. After `SIGCONT`, the pending handler ran,
   programmed the now-erased flag location, and rebooted.
7. SPL then read the exact requested words and U-Boot enumerated as HID
   `a108:ff08`.

The decisive UART excerpt was:

```text
Upgrade_flag0 = 0x55504454, Upgrade_flag1 = 0x10203a1
...
flash_flag1 = 0x55504454, flash_flag_2 = 0x010203a1
...
Upgrade mode = hid mode!!!
```

This proved that an erased target could accept the normal daemon's eight-byte
program and that stopping `ucamera` was the wrong ordering. The integrated path
therefore leaves `ucamera` running, performs no manual config erase, and keeps
the validated preparation and HID trigger inside one bounded operation.

## Bootloader transfer and full-flash write

Because the original CLI invocation had already exited, a one-off continuation
used the existing validated image/header builders and HID transfer function
against the waiting bootloader. It did not bypass image validation.

The complete 8 MiB image plus its 128-byte header required 2,742 packets. The
host observed the expected ACK sequence and final type-5 success. UART showed
all 16 received-image MD5 bytes matching, followed by the erase and write path:

```text
the whole firmware get,img size = 800000 ,img offset = 0
#####start erase flash#####
*****start write flash*****
Data transfer complete
```

The terminal type-5 success still proves only the RAM-image MD5; U-Boot sends
it before erase/write and does not check or report the SPI operation return
values. The independent readback below is what established write success.

## Independent post-write verification

After normal USB mode returned:

- the camera produced a live video picture;
- temporary root ADB startup succeeded through normal HID;
- `cc2flash backup` obtained three consecutive identical 8 MiB reads in three
  attempts;
- every byte from boot through the end of HWCONFIG (`0x000000–0x7dffff`)
  matched the validated candidate;
- the invariant firmware fingerprint and permanent startup-script patch passed
  strict validation;
- the live JFFS2 config contained the six expected names and no `system.sh`;
- config was not byte-identical to the initial clean image, as expected after
  first boot recreated default files.

Therefore the physical evidence validates the in-order bootloader HID transfer,
full-chip erase/write, normal reboot, video operation, and independent ADB
readback on this one camera.

## Integrated automatic restore validation

The follow-up test exercised PR #10 exactly as implemented, without manually
changing RAM, manually erasing config, stopping `ucamera`, or using a separate
transfer continuation:

1. `cc2flash start-adb --serial <camera>` started temporary root ADB.
2. `cc2flash restore ... --backup ... --serial <camera>` displayed the complete
   write plan and received the literal `RESTORE-CC2` confirmation.
3. The integrated preparation obtained three consecutive identical live flash
   reads, matched them to the preserved backup, dynamically located and verified
   the SFC object and its erase-size member, synchronized pending writes, and
   set/read back `0x1000`.
4. The stock HID trigger entered bootloader mode. All 2,742 transfer packets were
   accepted, U-Boot accepted the image MD5, and full-flash erase/write began.
5. Normal USB returned. After separately confirmed temporary ADB startup, the
   command obtained three consecutive identical post-write reads in three
   attempts.
6. Every boot-stable byte through HWCONFIG matched the candidate. Config changed
   during the required verification boot and was correctly not claimed
   byte-exact.

Sanitized host result:

```text
Validating the live camera and preparing its temporary SFC erase size.
Flash read 3/5 complete; consecutive identical: 3/3
Entering bootloader HID mode; the 8-byte flag write begins now.
Transfer: 100% (2742/2742)
Bootloader accepted the image MD5 and has started erase/write.
Normal mode returned; requiring three consecutive identical flash reads for post-write verification.
Flash read 3/5 complete; consecutive identical: 3/3
Restore complete: three consecutive post-write reads match every boot-stable byte through HWCONFIG.
```

The UART capture independently showed the flag erase/write, exact upgrade words,
SPL recognition, bootloader HID mode, and completed U-Boot write. No
`transfer length is error,803` occurred during the integrated flag operation.
The same error reappeared after Linux rebooted with its ordinary `0x4000`
configuration, confirming again that this preparation is intentionally temporary.

Therefore the integrated `cc2flash restore` path is physically verified end to
end on the supported camera and kernel: stable preflight readback, automatic RAM
preparation, stock trigger, stock bootloader transfer/write, reboot, live normal
USB, and independent stable post-write readback all succeeded.

## Remaining limits

- Physical validation covers one `EF-S7-V1.0.30B`/T23N/ZB25VQ64 camera and
  the exact gated stock kernel. Other board and firmware revisions remain
  unsupported unless separately analyzed and added.
- The live driver-object address must always be derived and verified at runtime.
  Hard-coding the address observed in either experiment would be unsafe.
- The decoded erase loop predicts one 4 KiB physical erase for the eight-byte
  flag operation, but that precise collateral footprint was not independently
  measured before the subsequent full-flash write. The preserved backup remains
  mandatory.
- Reboot restores the original `0x4000` configuration; this is preparation for
  the stock update transition, not a persistent fix for later JFFS2 erase/GC.
- Automatic retransmission is not implemented. Both successful physical
  transfers proceeded in order without requesting retransmission.

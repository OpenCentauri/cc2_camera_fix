# Experimental startup hooks without firmware flashing

This workflow is for the supported T23 camera while it still boots and has
working root ADB. It **writes files to the flash-backed config filesystem**;
it does not flash a firmware image or perform a raw partition erase. Keep a
verified backup from this camera and a working recovery method before testing.
Do not use it on a camera whose config is already exhausted.

The early-boot erase fix has physical validation on one supported camera:
hook execution, config remount, repeated boots, an observed GC erase and a
bounded automatic write/delete pressure test. See the
[hardware findings](STARTUP-HOOKS-VALIDATION.md) for observations and limits.
Offline tests additionally cover admission checks and shell failure paths;
power-loss recovery is not established. Installation never tries to remount
config while `ucamera` is running.

## Install from a checkout

Use the setup instructions in [INSTALLATION.md](INSTALLATION.md). Run from the
repository root so `python -m cc2camera` uses this checkout. Connect only one
camera. Obtain temporary ADB and preserve its backup first:

```sh
python -m cc2camera start-adb
python -m cc2camera backup before-hooks.zip
python -m cc2camera install-erase-fix --backup before-hooks.zip
```

Optional persistent ADB can be installed afterward, while ADB is online:

```sh
python -m cc2camera install-adb-startup --backup before-hooks.zip
```

Install the erase fix first so optional ADB does not consume its required space.
Persistent ADB is not required for the erase fix.

The confirmations are `INSTALL-ERASE-FIX` for the erase fix and `ENABLE-ADB`
for optional persistent ADB.
`--yes` explicitly supplies that consent for scripted local verification.
Both install commands accept `--adb` and `--serial`. They require already-online
ADB; neither silently starts it through HID. Keep the original backup unchanged.

Each command installs its feature in `/etc/conf.d/enabled`, and installs
`/etc/conf.d/system.sh` if absent:

| File | Purpose |
|---|---|
| `system.sh` | Copy the runner and all regular enabled scripts into a private tmpfs directory; execute them in C-locale filename order. |
| `enabled/10-erase-fix.sh` | Validate the known kernel and early-boot state, unmount config normally, correct the master erase size in RAM, and mount config again. |
| `enabled/90-adb.sh` | Start the existing ADB daemon if needed. |

Scripts execute synchronously with `/bin/sh`, before stock startup launches
`ucamera`. They must be regular files; symlinks and subdirectories in `enabled`
are refused by the runner. Hook stdout/stderr goes to `/tmp/cc2-hooks.log`.
A hook failure is logged and subsequent hooks can run if config is mounted.
If config is missing, read-only, or mounted from an unexpected device, the runner starts ADB for recovery and holds
startup rather than allowing the camera to run against an empty mountpoint.
It does not automatically reboot. This deliberate recovery hold lasts until
manual intervention; do not mistake it for a successful camera boot.

There is no migration or automatic overwrite of an existing different
`system.sh` or the selected managed hook. Only those two paths are checked for
expected contents by each installer. Unrelated regular hooks in `enabled/`
remain in place and execute in filename order. Exact files with mode `755`
are idempotent.
If an older or custom script is present, stop, save and review it, then resolve
it explicitly before installation. Do not blindly delete it or retry.

## Space admission and interrupted installation

Each command validates a preserved backup locally, then requires three
consecutive identical live flash reads, the known kernel SHA-256, matching
same-camera immutable data, and the expected partition map. It checks both:

- filesystem available bytes (`statfs`), with one extra logical erase block
  beyond the calculated installation budget;
- complete 16-KiB blocks containing a valid JFFS2 clean marker and otherwise
  only `FF`, with **five clean blocks retained** beyond the installation budget.

Dirty/reclaimable bytes, partial-block holes and entirely `FF` blocks without
clean markers do not count. Unmarked blocks may require the very erase operation
that is broken. The budget accounts for uncompressed data, inode pages,
namespace operations and block-tail slack. Current payloads require a 16-KiB
budget, hence at least six clean marked blocks and 32 KiB available via statfs.
This is deliberately conservative and may refuse a filesystem that could fit
these files under favorable conditions. There is no force option. If admission
fails for insufficient space, use the same-camera backup, image-building and
restore workflow instead. A runtime `force-gc`/unmount workaround is not provided.

The raw check measures already erased, marked blocks rather than assuming that
JFFS2 can reclaim deleted data. `statfs` alone is insufficient: its available
space accounting can include reclaimable dirty space. Neither measurement is
a guarantee against an erase failure, power loss or a concurrent writer.

Files are first pushed to verified tmpfs and read back byte-for-byte. Config is
read again and must be unchanged, and available space is checked again before
persistent writes. The selected hook is installed before the runner, using a
complete temporary file outside `enabled`, sync and rename. Final contents and
permissions are checked through ADB. No existing config file is truncated.

These checks cannot eliminate power-loss failure or a concurrent writer racing
the final check. Keep power stable. A timeout or readback failure can leave
partial installation; retain diagnostics and do not retry blindly. A leftover
`/etc/conf.d/.cc2-new-*` is refused and is never executed as an enabled hook.
No automatic cleanup writes config after an error.

## Verify on the camera

After the erase-fix command reports successful file readback, restart manually.
If persistent ADB was not installed, run `cc2camera start-adb` again to reconnect. Do not
run the erase hook yourself while the camera service is active.

```sh
adb shell cat /tmp/cc2-hooks.log
adb shell cat /proc/mtd
adb shell cat /proc/mounts
adb shell busybox pidof ucamera
adb shell busybox devmem 0x0043b190 32
```

Expected observations:

1. The log reports `10-erase-fix.sh` and contains
   `erase fix active; master=0x1000; partition geometry=0x4000`.
   If the optional ADB hook was installed, `10-erase-fix.sh` appears before
   `90-adb.sh`.
2. Config is mounted read/write from `/dev/mtdblock5` as JFFS2 at `/etc/conf.d`, and **all partition erase sizes
   still read `00004000`** in `/proc/mtd`. `ucamera` starts afterward.
3. If its optional hook was installed, ADB comes back without `start-adb`. The camera feed and its identity still work.
4. The final command returns a freshly read aligned pointer in
   `0x80450000..0x83fffd9c`. To independently read the master erase field,
   subtract `0x80000000` from that pointer and add `0x10`, then use
   `adb shell busybox devmem <calculated-address> 32` **without a value argument**.
   Expected value: `0x00001000`. Do not reuse an address from another boot.

Capture another backup and the kernel log without modifying config:

```sh
python -m cc2camera backup after-hooks.zip
adb shell dmesg
```

Stop if any check is refused, config is missing, the field or partition geometry
is wrong, ADB fails to return, the camera identity/feed changes, or dmesg reports
new erase/CRC/I/O errors. Do not run `flash_eraseall`, format config, kill
`ucamera` to force a remount, or repeatedly power-cycle a failing device.

Successful boot and readback alone establish hook execution, not garbage
collection. The separate [physical validation](STARTUP-HOOKS-VALIDATION.md)
records an observed erase, repeated boots and a bounded automatic GC pressure
test. Users do not need to repeat that stress test as routine installation
verification; the install commands do not stress the config partition.

## Recovery and compatibility

If the runner holds startup because config is missing or unusable, use the recovery ADB
connection to collect `/tmp/cc2-hooks.log`, `dmesg` and `/proc/mounts`. Do not
write to the empty `/etc/conf.d` directory. Use the preserved backup and the
repository's recovery procedure if ordinary mount recovery fails.

With config mounted and stable, removing an enabled hook is a persistent
filesystem operation requiring its own space and recovery assessment; removal
does not undo the current boot's RAM correction. Reboot restores the stock
master value unless the hook runs again. No uninstall command or migration is
provided here.

Full-flash backup still preserves these directories verbatim. The offline
`build-image --config-mode preserve-files` builder supports only flat config
files and **refuses** live subdirectories such as `enabled`; it does not silently
drop them. `serial-only` discards configuration by design and requires explicit
permission to discard unknown names. Do not use either as a hook-preserving
round trip. This workflow does not change the image builder's format support.

## Evidence and remaining uncertainty

Static analysis of all three supplied ROM images found the same kernel:
SHA-256 `0855c3a93f571c3130f1bdd38469e7c806ce713550a3ce536c81167579341545`
for flash bytes `[0x40000, 0x190000)`. The boot hook checks that complete kernel
partition with stock BusyBox MD5
`388e256470b2ad70f4a29cc37e0fee32`, plus exact live instructions, symbols,
partition geometry and the freshly read RAM pointer. MD5 here detects accidental
build differences; it does not authenticate firmware against an adversary.
The installer retains SHA-256. Using the built-in weaker boot fingerprint is an
explicit developer-approved exception; no other gate is relaxed.

The decoded driver selects erase commands for 4, 32 and 64 KiB. Its configured
16-KiB value falls through without initializing the command byte. A second
problem can replace a transfer failure with a later wait-ready success result.
These are static findings, not proof of which command reached the physical chip
on every failed erase. In particular, “the chip does not support 16 KiB” alone
is not an adequate explanation of the driver's behavior.

The hook changes only the master object's erase-size member, through the global
pointer at physical `0x0043b190`, offset `0x10`. Partition objects keep the copied
16-KiB geometry. The decoded loop therefore performs four 4-KiB operations for
a logical 16-KiB request. It does not alter JFFS2's block interpretation: existing
nodes can cross 4-KiB boundaries. The error-propagation defect is not repaired.

[Repository hardware records](../usb-maintenance/SFC-RESTORE-PREPARATION.md) report successful erase/readback after this RAM
field correction and return of failure after reboot. Those are prior reported
observations. The [startup-hook hardware findings](STARTUP-HOOKS-VALIDATION.md)
record physical runner execution, early remount and GC testing. The deliberate
recovery hold remains covered by offline tests, without physical fault injection.
The extracted stock `rcS` invokes `system.sh` after mounting config but before
`/home/bashrc.sh` launches `ucamera`; that establishes the proposed boot window.
Failure to mount config or find the script before that point cannot be repaired
by this hook.

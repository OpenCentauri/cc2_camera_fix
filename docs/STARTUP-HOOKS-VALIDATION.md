# Startup-hook hardware validation

These findings cover the supported Ingenic T23 camera with ZB25VQ64 flash
(JEDEC ID `5e4017`), Linux `3.10.14__isvp_pike_1.0__`, build #32 dated
2025-12-03, and PCB manufacturing code `1526`. Tests were performed by the
developer on one camera; private same-camera backup archives were compared
offline. No dumps or identity-bearing file contents are included here.

The tested camera already had the image-based preventive startup change that
copies defaults only when missing. Hook operation and the deliberate pressure
workload are physically tested on that setup. Hook-only operation with an
otherwise unmodified stock startup image was not separately tested.

## Installation and repeated boots: observed

The installer reported three consecutive identical flash reads, successful
hook-file installation/readback, and a reserved write budget of 16,384 bytes.
After reboot, the developer supplied:

```text
cc2flash: running 10-erase-fix.sh
cc2flash: erase fix active; master=0x1000; partition geometry=0x4000
cc2flash: 10-erase-fix.sh exit 0
cc2flash: running 90-adb.sh
cc2flash: 90-adb.sh exit 0
```

The same success log was reported after approximately 5–10 more reboots.
ADB returned automatically, config was mounted read/write as JFFS2, and
`ucamera` was running. The developer confirmed a working camera feed.
The supplied kernel log had no JFFS2 erase/CRC/I/O failure; unrelated USB/video
messages are not evidence about flash erasure.

All six `/proc/mtd` entries continued to report `00004000` erase size.
This is expected: the hook corrects the master driver's RAM field to 4 KiB,
while keeping the partition and filesystem geometry at 16 KiB. Successful
hook completion includes its normal unmount, RAM readback and mount checks.

The stock partition name is uppercase `HWCONFIG`. The earlier
`erase fix refused: partition-map` result was a case-sensitive admission
mismatch, not evidence that an attempted erase failed. The comparison now
normalizes case.

## Garbage collection: observed progression

The config partition is flash range `[0x7e0000, 0x800000)`: 128 KiB,
or eight logical 16-KiB blocks. A “clean block” below means a complete block
containing the valid JFFS2 clean marker followed only by erased `FF` bytes.
It does not mean every block containing deleted records has been reclaimed.

| Snapshot | Clean blocks | Direct observation |
|---|---:|---|
| After hook installation | 7 | Hook files persist; no erase transition in this comparison. |
| After a 16-KiB random write/readback/delete batch | 6 | Deleted test records consume space; developer reported success and no new kernel errors. |
| After 1 cumulative GC HUP request | 6 | An old clean marker becomes obsolete; no completed erase yet. |
| After 5 cumulative requests | 6 | Live records relocate; no completed erase yet. |
| After 13 cumulative requests | 5 | More live records relocate; first block still contains data. |
| After 25 cumulative requests | 6 | First logical block is erased and has a fresh clean marker. |
| After approximately 5–10 further reboots | 6 | All nine live file contents remain unchanged. |
| After 32 automatic pressure batches | 5 | All batches complete; live files remain unchanged. |

Manual requests sent SIGHUP to the identified `jffs2_gcd_mtd5` thread.
They were research operations, not a supported `force-gc` command. A request
need not finish an entire block: the intermediate snapshots show incremental
work. The kernel's GC implementation and
[upstream background thread](https://github.com/torvalds/linux/blob/v3.10/fs/jffs2/background.c)
support that interpretation.

The 25-request snapshot directly establishes an erase: 16,059 previously
programmed bytes in `[0x7e0000, 0x7e4000)` returned to `FF`, and the block
contains a fresh valid clean marker. All nine live files were preserved.
Sending a signal and seeing no kernel error alone would not establish this.

## Automatic pressure test: observation and inference

The test used 32 batches, each creating 8 KiB from `/dev/urandom` in tmpfs,
copying it to a dedicated config test file, calling `sync`, comparing contents,
deleting the test file, calling `sync` again and waiting one second.
It did not send manual GC signals or remount config.

Before writing, it checked the corrected master field and absence of the test
path. Each batch counted full clean blocks using raw MTD reads and a known
clean-block MD5. Stops were one or fewer clean blocks, command/readback
failure, matching kernel errors, or a maximum of 256 KiB cumulative payload.

The developer reported:

```text
Completed batches: 0; clean blocks: 6
Completed batches: 1; clean blocks: 6
Completed batches: 2; clean blocks: 5
...
Completed batches: 31; clean blocks: 5
CC2_PRESSURE_LIMIT_256_KIB
Final clean blocks: 5
```

Every intervening reported count after batch 2 was five. The final dump
contains obsolete test records for inode 18 and inode 49, consistent with the
first and final creations. There is no live pressure-test file.
All nine live files, including camera configuration and the three hook files,
are byte-for-byte identical to the after-reboots backup. Flash outside config
is also byte-for-byte identical.

**Inference:** successful synchronized random-data writes totaling twice the
partition capacity, with readback and a stable supply of clean blocks, strongly
support automatic reclamation keeping up with this workload.

**Limit of the dump comparison:** the two endpoint images contain no observed
0-to-1 bit transitions. A block that begins erased, is written and erased
during the test, and ends erased appears unchanged. These snapshots therefore
do not identify or count the individual automatic erase cycles. The direct
erase evidence comes from the separate manual-GC comparison above.

The test stopped at its workload limit, not near exhaustion. It did not
establish recovery from a nearly full initial filesystem, nor guarantee
indefinite endurance or survival under interrupted power.

## Space admission and the full-partition fallback

Installer admission combines filesystem availability with already erased,
clean-marker blocks. Dirty/reclaimable bytes do not satisfy the raw-space gate.
That distinction matters when erasure is broken: filesystem accounting alone
does not establish immediately writable physical space.

The current installation budget is one 16-KiB block, with five additional clean
blocks retained; statfs must report at least 32 KiB available. The pressure
experiment's lower stop threshold does not relax the installer's admission
policy. A successful install on this camera is not physical validation of
every borderline-space refusal or interrupted-write path.

For insufficient installation space, the chosen workflow is **same-camera
backup → patch image → restore**. No runtime `force-gc` route is provided.
The developer observed open `ucamera` descriptors for three config files;
stopping execution does not close those descriptors. SIGTERM is not a
freeze/resume mechanism, and successful ordinary unmount after stopping the
service was not established. Runtime service manipulation was abandoned.

## Evidence fingerprints and reproducibility

The following are SHA-256 hashes of the complete `flash.bin` members, not the
ZIP containers. Each archive passed the preserved-backup checks, including
the recorded three consecutive identical reads.

| Private snapshot label | SHA-256 |
|---|---|
| after-gc-hup-25 | `374220e6ee44bef82e1764e99debbf1f7b903eea4794e56d5b55a629e4a60129` |
| after-reboots | `502d0b3bee256a03658bb51173f6ed944c70e5eaafa298bc9bbe97bae540ff19` |
| after-auto-gc-pressure | `c4b324b5f350b0a66a0c070f1307d169a0686b57eeedc75b24f894f542f17d13` |

Read-only comparison method: validate each archive, compare bytes outside
config, split config into eight 16-KiB blocks, check exact clean-marker/FF
patterns, and scan CRC-valid JFFS2 nodes. Resolve latest directory entries
(inode zero means deletion) and reconstruct live file fragments by version.
An accurate node bit alone does not mean a record belongs to a live file.
Compare reconstructed file contents, and separately count bit transitions
and previously programmed bytes returning to FF.

This testing establishes successful operation on the stated setup, with direct
manual-GC erase evidence and strong automatic-GC evidence. Recovery hold under
an injected mount failure, power-loss behavior, near-full initial state and
hook-only operation on unmodified stock startup remain outside the physical
coverage. The separate driver error-propagation defect is not corrected by
the erase-size hook.

See [STARTUP-HOOKS.md](STARTUP-HOOKS.md) for installation and routine
verification. Repeating the pressure experiment is not required for normal use.

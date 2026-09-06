# CC2 camera recovery tool — independent verification

Review date: 2026-08-22  
Scope: independent audit of the recovery-image algorithm; current entry point: `cc2flash build-image`.

## Conclusion

The revised tool correctly recognizes both supplied bricked CC2 camera ROMs, preserves their distinct unit identities, rebuilds their exhausted JFFS2 config partitions, and installs the intended copy-once startup mitigation. It is fail-closed for invariant firmware changes and produces the exact recovery hashes already recorded for both units.

The software-level recovery claim is verified. A new hardware-level recovery claim is not possible from files alone: definitive proof still requires writing a physical bricked camera, obtaining a byte-identical full readback, and observing successful boot/USB enumeration.

## Direct evidence

| Property | Bricked unit A | Bricked unit B |
|---|---:|---:|
| Input SHA-256 | `4325aebe84d70dd937de1790aa48f4b36ee2731def0ee5504ed080e4df13e819` | `ff9c8962abd06a14661db1857f6318b06f6378e93c4d812d96224094063bbcf9` |
| Config non-`FF` use | 98.567% | 98.486% |
| CRC-valid JFFS2 nodes | 630 | 655 |
| Obsolete nodes | 628 | 653 |
| Live names | `serial.cfg` only | `serial.cfg` only |
| Generated SHA-256 | `5939d62a32fd97ab15a5d9b8ab71571831f0f2a30d66019c23deef892905ec09` | `269f1b3b205e2ac30ada7cb98a7aeb9ada2e76786dc14abe95ea9c56dce73d1f` |

The inputs have different serial values, UOIDs, two-byte HWCONFIG check values, and raw config histories. Every supposedly invariant byte has the same fingerprint. For each output:

- boot, kernel, root, all unmodified system bytes, and the complete HWCONFIG partition remain byte-for-byte equal to that unit's input;
- the exact CRC-valid `serial.cfg` payload recovered from that unit is preserved;
- config becomes the canonical minimal JFFS2 image;
- the generated ROM passes the complete validator again.

Unit B's generated SHA-256 equals the permanent hardware-readback hash recorded in the earlier test results. The raw readback was not provided in this review, so that historical file itself could not be independently compared.

## Does the patch fix the stated bug?

Yes, for the specific deterministic boot-write mechanism.

The stock `/home/bashrc.sh` executes five unconditional `cp` commands into the JFFS2-backed `/etc/conf.d` on every boot. JFFS2 is log structured, so rewriting the same logical files still appends new nodes and obsoletes older ones. Both bricked dumps exhibit the expected end state: roughly 98.5% physical use and hundreds of obsolete nodes.

The patch changes each command to the POSIX-shell form:

```sh
[ -f /etc/conf.d/file ] || cp /system/config/file /etc/conf.d/file
```

After recovery, the minimal config contains only `serial.cfg`. The five defaults are therefore created once on the first successful startup. On later boots their destination files exist, so those five persistent writes stop. The patched script has the same 5,699-byte length as stock and passes `sh -n`.

This mitigates the observed write leak; it does not fix the underlying Ingenic SFC/JFFS2 erase or garbage-collection defect. Other persistent writers could still consume the partition.

## Does it recover an already bricked image?

At the image level, yes. Both supplied bricked configs retain one unambiguous CRC-valid serial but are almost physically exhausted. The builder replaces that log with a small CRC-valid JFFS2 partition containing the same serial and ample erased space, then applies the permanent startup mitigation.

The result for both real units is deterministic and matches the previously documented output hashes. This is sufficient to verify image construction, identity preservation, and filesystem consistency. Only an actual flash/readback/boot test can establish electrical and runtime recovery for a particular board.

## Cross-serial behavior and residual risk

Cross-serial support was directly verified on two real units and structurally tested on a third synthetic identity.

The validator excludes only the known unit-specific HWCONFIG fields and mutable config log from invariant comparison. It still requires:

- exact firmware hashes everywhere else;
- the exact stock or audited patched SquashFS window;
- a 94-byte UOID with the observed character structure;
- exactly one recoverable serial matching the observed serial format;
- the observed 12-byte common prefix between serial and UOID;
- valid JFFS2 CRCs and only known config filenames.

For an unseen full-ROM hash, v1.1 requires three byte-identical physical reads by default. `--allow-fewer-reads` is an explicit reduced-confidence escape hatch and is recorded with a warning in generated artifacts.

The proprietary full serial↔UOID derivation and the meaning of the two-byte HWCONFIG check value remain unknown. The tool therefore preserves all of those bytes exactly but cannot cryptographically prove their full mutual relationship. This is the principal residual cross-device identity risk. Requiring multiple physical reads minimizes corruption risk without inventing an unverified formula.

## Auditability improvements in v1.1

The earlier ~32 KiB Base85-encoded XOR blob was removed. The patch is now produced from:

1. the complete readable old and new shell blocks;
2. exact hashes for the stock XZ stream, decompressed fragment, stock script, patched script, patched decompressed fragment, patched XZ stream, and final 32 KiB window;
3. deterministic standard-library LZMA2/XZ construction with a 128 KiB dictionary;
4. one explicit SquashFS fragment-size update.

An independent read-only SquashFS v4/XZ parser extracted `bashrc.sh` from the stock and generated ROMs. The extracted files were byte-for-byte equal to the supplied original and patched scripts, respectively.

Additional safety changes:

- non-reference devices require three matching reads by default;
- generated recovery images are strictly validated before programmer verification;
- existing output directories are refused without deleting their contents;
- generated Bus Pirate examples use documented `dev` and `spispeed` parameters and do not silently enable target power. See the [official flashrom Bus Pirate documentation](https://flashrom.org/supported_hw/supported_prog/buspirate.html).

## Regression results

All executed tests passed:

- both actual bricked inputs generated their exact expected outputs;
- an already patched canonical output rebuilt idempotently with no write region;
- a structurally valid unseen serial/UOID with three matching reads was accepted and preserved;
- one-bit mutations in kernel or the system patch window were rejected;
- a serial from one real unit combined with the other unit's UOID was rejected;
- keeping either exhausted config was rejected;
- a one-bit programmer readback mismatch was rejected;
- the deterministic XZ, readable source replacement, JFFS2 writer/parser, strict post-build validation, and shell syntax checks all passed.

## Recommended hardware validation sequence

1. Make three full reads without moving the clip; require identical SHA-256 hashes.
2. Run `inspect-image`, then `build-image` with the other two reads supplied via `--confirm-read`.
3. Verify the exact flash part voltage and all SPI connections from the marking/datasheet.
4. Write only the generated layout regions. Do not power the target simultaneously from USB/device power and programmer target power.
5. Without disturbing the connection, make a full 8 MiB readback.
6. Compare every readback byte with the generated image using `cmp` or Windows `fc.exe /b`.
7. Disconnect the programmer, restore normal power, and observe boot and USB enumeration.

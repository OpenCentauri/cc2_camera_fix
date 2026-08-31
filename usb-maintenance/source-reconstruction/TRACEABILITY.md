# Traceability and confidence

## Linux `/bin/hid_update`

| Reconstructed function | Original VA | Evidence represented |
|---|---:|---|
| `linux_main` | `0x00400b90` | two detached threads; main sleeps forever |
| `process_watch_thread` | `0x00400e90` | `ps > /tmp/1.txt`; scans for `ucamera`; 15-second loop |
| `cc2_crc16` | `0x00400fe8` | reflected right-shift loop using `0x1021`, initial `0xffff` |
| `send_normal_response` | `0x00401034` | 1,024-byte report construction, CRC, write |
| `dispatch_normal_report` | `0x0040123c` | validation, status codes, top-level command groups |
| `hid_event_thread` | `0x004017d4` | device opens, upgrade-word cleanup, `select`/read loop |
| `probe_ucamera_ready` | `0x00401b94` | `/dev/ucamera` read and ioctl `0x20005508` |
| `config_get` | `0x00401cd0` | command/key lookup and value response |
| `config_set` | `0x00402038` | tail-buffered configuration rewrite and zero-padding |
| `dispatch_config` | `0x004024c0` | all 42 GET/SET commands |
| `upload_init` | `0x00402830` | 16 MiB + 512 allocation and field reset |
| `upload_append` | `0x004028dc` | sequence/capacity checks and final flag |
| `upload_commit` | `0x004029f0` | remove/write/chmod/backup behavior |
| `build_sensor_path` | `0x00402ba8` | `sensor_name` lookup and two derived paths |
| `dispatch_upload` | `0x00402e3c` | full `0x3xxx` command state machine |

The ELF's `.pdr` records provide the function starts above even though ordinary
symbols were stripped. PLT relocations identify the libc and pthread calls.

## SPL and main U-Boot

The main U-Boot image begins at flash file offset `0x6800` and is linked at
`0x80100000`, so `VA = file_offset - 0x6800 + 0x80100000`.

| Reconstructed function | Original VA | Evidence represented |
|---|---:|---|
| SPL upgrade-word read | `0xf0001c28` | reads 8 bytes at flash `0x7f8000` |
| SPL magic comparison | `0xf0001c54` | compares word 0 with `0x55504454` |
| `validate_host_frame` | `0x80117948` | host magic and additive checksum |
| `build_device_frame` | `0x801179ec` | response magic, length, payload, checksum |
| retry/ACK helper | `0x80117b28` | batch ACK and five-error abort behavior |
| `updater_receive` | `0x80117d1c` | type-1 metadata and type-3 data branches |
| SPI updater initialization | `0x801186ac` | SPI probe and function-table setup |
| `select_update_transport` | `0x80118714` | persistent `a0`/`a1` mode selection |
| MD5 calculation | `0x80119620` | image-data MD5 |
| HID envelope | `0x8011b9d8` | report ID 1 and 3,072-byte HID reports |

## Important omissions and bug-compatible behavior

These findings are especially useful when verifying host/client assumptions:

1. Neither USB updater implements SPI-flash readback.
2. Any normal-mode command in the `0x4xxx` group reaches the reboot/upgrade path.
3. Normal-mode CRC length is trusted before being bounded to the report.
4. Linux file-upload paths are host-controlled for most `0x31xx` commands and
   are passed through shell strings (`rm %s`, `chmod 0777 %s`) without quoting.
5. Upload state checks also accept state `5` for `0x3200` and `0x3300` because
   the compiled arithmetic test is flawed.
6. U-Boot checks metadata magic/type but calls the common validator without using
   its result before copying the 10-byte metadata payload.
7. U-Boot does not reject zero subpacket size, zero packets-per-ACK, oversized
   transfer size, or inconsistent header/metadata lengths.
8. U-Boot compares MD5 only over `header.image_length` bytes beginning at offset
   128, without proving the allocation contains that range.
9. U-Boot sends the type-5 success byte before SPI erase/write and does not check
   erase/write results or perform a readback.
10. The SPI target range is not checked against the 8 MiB chip boundary.

## What remains inferred or runtime-unverified

- Decompiled local/global names and aggregate types.
- Exact source syntax and original function boundaries for inlined helpers.
- CDC runtime PID and endpoint assignments.
- Normal and bootloader endpoint addresses selected at runtime.
- Hardware timing, disconnect/re-enumeration timing, and physical write outcome.

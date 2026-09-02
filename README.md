# Elegoo CC2 camera recovery tools

This repository collects two complementary approaches for maintaining and
recovering the stock Elegoo Centauri Carbon 2 camera. They share a repository
for now but remain separate tools with independent workflows and safety notes.

## [Hardware recovery](hardware-recovery/)

Repairs a camera from a validated, device-specific SPI flash dump using an
external programmer. It can rebuild an exhausted JFFS2 configuration partition
and patch the recurring boot-time write leak, including for cameras that no
longer boot normally. It accepts either raw 8 MiB programmer dumps or the
validated ZIP archives produced by USB maintenance.

## [USB maintenance](usb-maintenance/)

Documents the recovered Linux HID and U-Boot updater protocols and provides the
`cc2flash` Python package for command construction, guarded ADB startup, and
three-consecutive-read flash backup. It can validate and restore the recovery
image produced from its preserved backup. Hardware recovery offers a clean-data
rebuild or a preserve-data rebuild that writes each live config file once.
Destructive USB restore remains
hardware-unverified; read the subproject status and protocol documents before
using write operations.

No ROM image is included. Both approaches operate on device-specific data, so
preserve independent backups and follow the safety requirements in the relevant
subproject README.

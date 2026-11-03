# CC2 camera hardware recovery

Start with the repository's
[camera identification and recovery guide](../README.md#first-identify-your-camera).
It determines whether this repair applies.

If the camera no longer starts and you can force USB boot, prefer the
[USB-boot recovery route without a programmer](../docs/USB-BOOT-RECOVERY.md)
(short flash pins 5 and 6 at power-up, then read/write with thingino-dfu and
`cc2camera build-image`). This directory documents the external-programmer
(NeoProgrammer / CH341) workflow with one required command-line invocation.

For details beyond that programmer path:

- [CLI reference](../docs/CLI.md) documents inspection, alternate build
  modes, output files, and independent readback verification.
- [Technical details](TECHNICAL_DETAILS.md) document the root cause, exact
  firmware transformation, supported image family, identity handling, safety
  properties, and limitations.
- [Programmer reference](PROGRAMMER_REFERENCE.md) contains the complete
  electrical mapping and the Bus Pirate alternative.
- [Independent verification](CC2_RECOVERY_VERIFICATION.md) and
  [regression results](TEST_RESULTS.md) record the current evidence.



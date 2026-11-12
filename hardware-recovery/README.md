# CC2 camera hardware recovery

Start with the repository's
[camera identification and recovery guide](../README.md#first-identify-your-camera).
It determines whether this repair applies and presents the normal
NeoProgrammer workflow with one required command-line invocation.

If you do not have an external programmer and want to try recovery over USB
boot instead, see the optional
[USB-boot fallback](../docs/USB-BOOT-RECOVERY.md). Prefer this directory's
programmer path when you can: a CH341-style setup includes voltage checks and
isolation that a homemade camera-to-USB cable plugged into a laptop does not.

For details beyond that normal path:

- [CLI reference](../docs/CLI.md) documents inspection, alternate build
  modes, output files, and independent readback verification.
- [Technical details](TECHNICAL_DETAILS.md) document the root cause, exact
  firmware transformation, supported image family, identity handling, safety
  properties, and limitations.
- [Programmer reference](PROGRAMMER_REFERENCE.md) contains the complete
  electrical mapping and the Bus Pirate alternative.
- [Independent verification](CC2_RECOVERY_VERIFICATION.md) and
  [regression results](TEST_RESULTS.md) record the current evidence.

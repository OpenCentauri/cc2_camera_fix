"""Offline deterministic image-generation checks (no public CLI command)."""
import io
import json
import lzma
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cc2flash import image as tool
from cc2flash.image import (
    ORIGINAL_COPY_BLOCK, PATCHED_COPY_BLOCK_VISIBLE, ValidationError,
    build_squashfs_xz_fragment, serial_payload_is_valid, build_minimal_config,
    extract_serial_and_config_info, read_image_source, analyze_image,
    build_system_patch_window, sha256, PATCHED_PATCH_SHA256, TOOL_VERSION,
)

def implementation_checks(image_path: Path | None = None) -> None:
    padding = len(ORIGINAL_COPY_BLOCK) - len(PATCHED_COPY_BLOCK_VISIBLE)
    if padding < 0:
        raise ValidationError("Readable bashrc patch block is too large")
    replacement = PATCHED_COPY_BLOCK_VISIBLE[:-1] + b" " * padding + b"\n"
    if len(replacement) != len(ORIGINAL_COPY_BLOCK):
        raise ValidationError("Readable bashrc patch does not preserve length")

    xz_vector = (b"CC2 auditable SquashFS patch self-test\n" * 257) + b"end"
    xz_stream = build_squashfs_xz_fragment(xz_vector)
    if lzma.decompress(xz_stream) != xz_vector:
        raise ValidationError("Deterministic XZ builder self-test failed")

    dummy = b"serial=12PSSSS4TEST000000000000000000000000\n"
    # Dummy must retain the exact 36-character serial value format.
    if not serial_payload_is_valid(dummy):
        raise ValidationError("Internal serial regex self-test vector is invalid")
    config = build_minimal_config(dummy)
    parsed = extract_serial_and_config_info(config)
    if parsed["serial_payload"] != dummy:
        raise ValidationError("JFFS2 self-test failed")

    image_result = "not requested"
    if image_path is not None:
        image, source = read_image_source(image_path)
        source_name = str(image_path)
        if source["image_member"] is not None:
            source_name += f"!{source['image_member']}"
        analysis = analyze_image(image, source_name)
        if analysis["system_state"] == "stock-unpatched":
            patched = build_system_patch_window(image)
            if sha256(patched) != PATCHED_PATCH_SHA256:
                raise ValidationError("Full system patch self-test failed")
            image_result = "stock image generated the exact audited patch window"
        else:
            image_result = "image already contains the exact audited patch window"

    print("SELF-TEST PASSED")
    print(f"Tool version: {TOOL_VERSION}")
    print(f"Readable bashrc replacement padding: {padding} bytes")
    print(f"Deterministic XZ test SHA-256: {sha256(xz_stream)}")
    print(f"Generated JFFS2 SHA-256: {sha256(config)}")
    print(f"Image patch test: {image_result}")



class ImplementationTests(unittest.TestCase):
    def test_deterministic_patch_xz_and_jffs2(self):
        with redirect_stdout(io.StringIO()):
            implementation_checks()

    def test_serial_and_uoid_are_validated_independently(self):
        image = bytearray(b"\0" * tool.FLASH_SIZE)
        uoid = b"12PSSSS4DIFF" + b"A" * 82
        image[tool.HW_UOID_START:tool.HW_UOID_END] = uoid
        image[tool.HW_CHECK_START:tool.HW_CHECK_END] = b"\x01\x02"

        serial_payload = (
            b"serial=12PSSSS4TEST000000000000000000000000\n"
        )
        serial_value = serial_payload.removeprefix(b"serial=").removesuffix(b"\n")
        self.assertNotEqual(serial_value[:12], uoid[:12])
        image[tool.CONFIG_START:tool.CONFIG_END] = tool.build_minimal_config(
            serial_payload
        )
        image = bytes(image)

        variant = {
            "before_identity_sha256": tool.sha256(
                image[0x7D0000:tool.HW_CHECK_START]
            ),
            "after_uoid_sha256": tool.sha256(
                image[tool.HW_UOID_END:tool.CONFIG_START]
            ),
            "invariant_sha256": tool.sha256(tool.invariant_bytes(image)),
        }

        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary = root / "primary.bin"
            confirmation_one = root / "confirmation-one.bin"
            confirmation_two = root / "confirmation-two.bin"
            for path in (primary, confirmation_one, confirmation_two):
                path.write_bytes(image)

            output = root / "recovery"
            with (
                mock.patch.object(tool, "REFERENCE_SEGMENTS", {}),
                mock.patch.object(
                    tool,
                    "PATCHED_PATCH_SHA256",
                    tool.sha256(image[tool.PATCH_START:tool.PATCH_END]),
                ),
                mock.patch.object(
                    tool,
                    "identify_hwconfig_variant",
                    return_value=("synthetic", variant),
                ),
            ):
                result = tool.build_recovery(
                    primary,
                    output,
                    confirmation_paths=(confirmation_one, confirmation_two),
                    config_mode="serial-only",
                    wipe_unknown_config=False,
                    show_serial=False,
                    allow_fewer_reads=False,
                )

            recovery = (output / "cc2-camera-recovery.bin").read_bytes()
            self.assertEqual(
                recovery[0x7D0000:tool.CONFIG_START],
                image[0x7D0000:tool.CONFIG_START],
            )
            self.assertEqual(recovery[tool.HW_UOID_START:tool.HW_UOID_END], uoid)
            self.assertEqual((output / "serial.cfg").read_bytes(), serial_payload)
            rebuilt = tool.extract_serial_and_config_info(
                recovery[tool.CONFIG_START:tool.CONFIG_END]
            )
            self.assertEqual(rebuilt["serial_payload"], serial_payload)
            self.assertNotIn("serial_uoid_prefix_match", result["analysis"])
            self.assertNotIn("serial_uoid_prefix_match", result["output_analysis"])
            self.assertNotIn(
                "Serial/UOID prefix",
                (output / "VALIDATION.txt").read_text(encoding="utf-8"),
            )
            generated_manifest = json.loads(
                (output / "MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "serial_uoid_prefix_match", generated_manifest["validation"]
            )

"""Offline deterministic image-generation checks (no public CLI command)."""
import io
import lzma
from pathlib import Path
import unittest
from contextlib import redirect_stdout
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


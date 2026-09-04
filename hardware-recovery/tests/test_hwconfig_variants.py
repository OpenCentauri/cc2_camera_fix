from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


TOOL_PATH = Path(__file__).parents[1] / "cc2_sig_tool.py"
SPEC = importlib.util.spec_from_file_location("cc2_sig_tool_hwconfig", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


def hwconfig_record(length: int, extension: bytes = b"") -> bytes:
    image = bytearray(b"\0" * tool.FLASH_SIZE)
    image[tool.HW_RECORD_START:tool.HW_RECORD_START + 2] = (12).to_bytes(2, "little")
    image[tool.HW_RECORD_START + 2:tool.HW_RECORD_PAYLOAD_START] = length.to_bytes(
        2, "little"
    )
    image[
        tool.HW_KNOWN_PAYLOAD_END:tool.HW_KNOWN_PAYLOAD_END + len(extension)
    ] = extension
    return bytes(image)


class HwconfigVariantTests(unittest.TestCase):
    def test_original_256_byte_record_is_supported(self):
        name, _ = tool.identify_hwconfig_variant(hwconfig_record(0x100))
        self.assertEqual(name, "type12-length256")

    def test_observed_261_byte_record_and_exact_extension_are_supported(self):
        name, _ = tool.identify_hwconfig_variant(
            hwconfig_record(0x105, bytes.fromhex("0000029840"))
        )
        self.assertEqual(name, "type12-length261-trailer-0000029840")

    def test_arbitrary_261_byte_extension_is_rejected(self):
        with self.assertRaisesRegex(tool.ValidationError, "unsupported exact shape"):
            tool.identify_hwconfig_variant(
                hwconfig_record(0x105, bytes.fromhex("0000029841"))
            )

    def test_unobserved_record_length_is_rejected(self):
        with self.assertRaisesRegex(tool.ValidationError, "payload_length=262"):
            tool.identify_hwconfig_variant(
                hwconfig_record(0x106, bytes.fromhex("000002984000"))
            )

    def test_non_type12_record_is_rejected(self):
        image = bytearray(hwconfig_record(0x100))
        image[tool.HW_RECORD_START:tool.HW_RECORD_START + 2] = (13).to_bytes(2, "little")
        with self.assertRaisesRegex(tool.ValidationError, "type=13"):
            tool.identify_hwconfig_variant(bytes(image))


if __name__ == "__main__":
    unittest.main()

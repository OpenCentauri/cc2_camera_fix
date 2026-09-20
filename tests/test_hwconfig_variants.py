from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


from cc2camera import image as tool


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
    def test_observed_identity_prefixes_are_supported(self):
        for family in (b"3", b"4"):
            with self.subTest(family=family.decode("ascii")):
                serial = b"serial=12PSSSS" + family + b"A" * 28 + b"\n"
                uoid = b"12PSSSS" + family + b"A" * 86
                self.assertTrue(tool.serial_payload_is_valid(serial))
                self.assertIsNotNone(tool.UOID_PATTERN.fullmatch(uoid))

    def test_unobserved_identity_prefixes_are_rejected(self):
        for family in (b"2", b"5"):
            with self.subTest(family=family.decode("ascii")):
                serial = b"serial=12PSSSS" + family + b"A" * 28 + b"\n"
                uoid = b"12PSSSS" + family + b"A" * 86
                self.assertFalse(tool.serial_payload_is_valid(serial))
                self.assertIsNone(tool.UOID_PATTERN.fullmatch(uoid))

    def test_original_256_byte_record_is_supported(self):
        name, record = tool.identify_hwconfig_variant(hwconfig_record(0x100))
        self.assertEqual(name, "type12-length256")
        self.assertEqual(record["extension_length"], 0)

    def test_observed_261_byte_record_is_supported(self):
        name, record = tool.identify_hwconfig_variant(
            hwconfig_record(0x105, bytes.fromhex("0000029840"))
        )
        self.assertEqual(name, "type12-length261")
        self.assertEqual(record["extension_length"], 5)

    def test_unknown_five_byte_extension_content_is_supported(self):
        extension = bytes.fromhex("deadbeef01")
        name, record = tool.identify_hwconfig_variant(
            hwconfig_record(0x105, extension)
        )
        self.assertEqual(name, "type12-length261")
        self.assertEqual(record["extension_length"], len(extension))
        self.assertEqual(record["extension_sha256"], tool.sha256(extension))

    def test_extension_is_normalized_without_hiding_later_mutations(self):
        original = hwconfig_record(0x100)
        observed = hwconfig_record(0x105, bytes.fromhex("0000029840"))
        changed_trailer = hwconfig_record(0x105, bytes.fromhex("0000029841"))
        extended = hwconfig_record(0x105, bytes.fromhex("deadbeef01"))
        _, original_record = tool.identify_hwconfig_variant(original)
        _, observed_record = tool.identify_hwconfig_variant(observed)
        _, changed_trailer_record = tool.identify_hwconfig_variant(changed_trailer)
        _, extended_record = tool.identify_hwconfig_variant(extended)
        normalized = tool.invariant_bytes(original, original_record)
        for image, record in (
            (observed, observed_record),
            (changed_trailer, changed_trailer_record),
            (extended, extended_record),
        ):
            self.assertEqual(normalized, tool.invariant_bytes(image, record))

        mutated = bytearray(extended)
        mutated[extended_record["record_end"] + 1] = 1
        self.assertNotEqual(
            tool.invariant_bytes(original, original_record),
            tool.invariant_bytes(bytes(mutated), extended_record),
        )

    def test_unit_fields_are_normalized_without_hiding_neighbors(self):
        original = bytearray(hwconfig_record(0x100))
        original[tool.HW_CHECK_START:tool.HW_CHECK_END] = b"\x01\x02\x03"
        original[tool.HW_UOID_START:tool.HW_UOID_END] = (
            b"12PSSSS4" + b"A" * 86
        )
        original[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((4, 1))
        )

        observed = bytearray(hwconfig_record(0x105, bytes.fromhex("0000000840")))
        observed[tool.HW_CHECK_START:tool.HW_CHECK_END] = b"\x04\x05\x06"
        observed[tool.HW_UOID_START:tool.HW_UOID_END] = (
            b"12PSSSS3" + b"B" * 86
        )
        observed[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((3, 2))
        )

        original = bytes(original)
        observed = bytes(observed)
        _, original_record = tool.identify_hwconfig_variant(original)
        _, observed_record = tool.identify_hwconfig_variant(observed)
        self.assertEqual(
            tool.invariant_bytes(original, original_record),
            tool.invariant_bytes(observed, observed_record),
        )

        for offset in (tool.HW_CHECK_START - 1, tool.HW_DATE_END):
            mutated = bytearray(observed)
            mutated[offset] ^= 1
            with self.subTest(offset=f"0x{offset:06X}"):
                self.assertNotEqual(
                    tool.invariant_bytes(original, original_record),
                    tool.invariant_bytes(bytes(mutated), observed_record),
                )

    def test_hwconfig_date_is_validated(self):
        image = bytearray(hwconfig_record(0x100))
        image[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((3, 2))
        )
        self.assertEqual(tool.decode_hwconfig_date(bytes(image)), "2026-03-02")

        image[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((4, 1))
        )
        self.assertEqual(tool.decode_hwconfig_date(bytes(image)), "2026-04-01")

        image[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((2, 30))
        )
        with self.assertRaisesRegex(tool.ValidationError, "not a valid"):
            tool.decode_hwconfig_date(bytes(image))

        image[tool.HW_DATE_START:tool.HW_DATE_END] = (
            (2026).to_bytes(2, "little") + bytes((3, 3))
        )
        with self.assertRaisesRegex(tool.ValidationError, "physically observed"):
            tool.decode_hwconfig_date(bytes(image))

    def test_short_known_payload_is_rejected(self):
        with self.assertRaisesRegex(tool.ValidationError, "shorter"):
            tool.identify_hwconfig_variant(
                hwconfig_record(0xFF)
            )

    def test_unobserved_payload_lengths_are_rejected(self):
        for length in (0x101, 0x106, 0x200):
            with self.subTest(length=length):
                with self.assertRaisesRegex(
                    tool.ValidationError, "physically observed"
                ):
                    tool.identify_hwconfig_variant(hwconfig_record(length))

    def test_non_type12_record_is_rejected(self):
        image = bytearray(hwconfig_record(0x100))
        image[tool.HW_RECORD_START:tool.HW_RECORD_START + 2] = (13).to_bytes(2, "little")
        with self.assertRaisesRegex(tool.ValidationError, "type 13"):
            tool.identify_hwconfig_variant(bytes(image))


if __name__ == "__main__":
    unittest.main()

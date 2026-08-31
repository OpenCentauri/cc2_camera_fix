from __future__ import annotations

import struct
import unittest

from cc2flash.commands import (
    ADB_UPLOAD_COMMAND_TARGET,
    BOOT_FRAME_TYPES,
    CONFIGURATION_COMMANDS,
    DERIVED_UPLOAD_TARGET_COMMANDS,
    LITERAL_UPLOAD_TARGET_COMMANDS,
    NORMAL_COMMAND_CATALOG,
    NORMAL_DISPATCH_MAP,
    UPLOAD_COMMANDS,
    BootFrameType,
    ConfigurationKey,
    NormalCommand,
    NormalCommandFamily,
    NormalFrameType,
    build_boot_data_request,
    build_boot_metadata_request,
    build_adb_upload_command_target_request,
    build_config_get_request,
    build_config_set_request,
    build_upgrade_request,
    build_upload_commit_request,
    build_upload_data_request,
    build_upload_initialize_request,
    build_upload_target_request,
    build_version_request,
    decode_boot_ack,
    decode_boot_terminal_status,
    describe_normal_command,
)
from cc2flash.protocol import (
    BOOT_DATA_SIZE,
    BOOT_REPORT_SIZE,
    ProtocolError,
    UpdatePlan,
    additive_checksum,
    parse_boot_frame,
    parse_normal_report,
)


def device_frame(frame_type: int, payload: bytes) -> bytes:
    result = bytearray(len(payload) + 7)
    result[:3] = b"\x81\x00\xee"
    result[3] = frame_type
    struct.pack_into("<H", result, 4, len(result))
    result[6:-1] = payload
    result[-1] = additive_checksum(result[:-1])
    return bytes(result)


class CommandCatalogTests(unittest.TestCase):
    def test_catalog_contains_every_exact_recovered_normal_command(self):
        # 1 version + 42 configuration + 13 upload + 1 canonical upgrade.
        self.assertEqual(len(NormalCommand), 57)
        self.assertEqual(len(NORMAL_COMMAND_CATALOG), 57)
        self.assertEqual(
            {int(command) for command in NormalCommand},
            {int(entry.command) for entry in NORMAL_COMMAND_CATALOG},
        )
        self.assertEqual(len({int(entry.command) for entry in NORMAL_COMMAND_CATALOG}), 57)

    def test_configuration_table_matches_protocol_document(self):
        expected = (
            (0x2000, 0x2001, "manufact_lab", True),
            (0x2010, 0x2011, "serial_lab", True),
            (0x2020, 0x2021, "productnumber", False),
            (0x2030, 0x2031, "vendor_id", True),
            (0x2040, 0x2041, "product_id", True),
            (0x2050, 0x2051, "device_bcd", True),
            (0x2F00, 0x2F01, "model_lab", False),
            (0x2F10, 0x2F11, "cmei_lab", False),
            (0x2F20, 0x2F21, "appkey", False),
            (0x2F30, 0x2F31, "product_lab", True),
            (0x2F40, 0x2F41, "adb_en", True),
            (0x2F50, 0x2F51, "sensor_name", True),
            (0x2F60, 0x2F61, "i2c_addr", True),
            (0x2F70, 0x2F71, "default_boot", True),
            (0x2F80, 0x2F81, "sensor_fps", True),
            (0x2F90, 0x2F91, "sensor_width", True),
            (0x2FA0, 0x2FA1, "sensor_height", True),
            (0x2FB0, 0x2FB1, "hvflip", True),
            (0x2FC0, 0x2FC1, "rcmode", True),
            (0x2FD0, 0x2FD1, "bitrate", True),
            (0x2FE0, 0x2FE1, "qp_value", True),
        )
        actual = tuple(
            (
                int(entry.get),
                int(entry.set),
                entry.key.value,
                entry.present_in_supplied_active_config,
            )
            for entry in CONFIGURATION_COMMANDS
        )
        self.assertEqual(actual, expected)

    def test_upload_table_has_all_thirteen_commands(self):
        expected = {
            0x3000,
            0x3110,
            0x3111,
            0x3112,
            0x3113,
            0x3114,
            0x3115,
            0x3116,
            0x3120,
            0x3150,
            0x31F0,
            0x3200,
            0x3300,
        }
        self.assertEqual({int(entry.command) for entry in UPLOAD_COMMANDS}, expected)
        self.assertEqual(len(LITERAL_UPLOAD_TARGET_COMMANDS), 8)
        self.assertEqual(len(DERIVED_UPLOAD_TARGET_COMMANDS), 2)

    def test_dispatch_map_covers_every_16_bit_value_without_gaps(self):
        self.assertEqual(NORMAL_DISPATCH_MAP[0].first, 0)
        self.assertEqual(NORMAL_DISPATCH_MAP[-1].last, 0xFFFF)
        for left, right in zip(NORMAL_DISPATCH_MAP, NORMAL_DISPATCH_MAP[1:]):
            self.assertEqual(left.last + 1, right.first)

    def test_group_wide_upgrade_lookup(self):
        entry = describe_normal_command(0x4ABC)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.command, 0x4ABC)
        self.assertEqual(entry.family, NormalCommandFamily.UPGRADE_TRIGGER)
        self.assertIsNone(describe_normal_command(0x1000))


class NormalCommandBuilderTests(unittest.TestCase):
    def test_version_builder(self):
        frame = parse_normal_report(build_version_request())
        self.assertEqual(frame.command, 0x0001)
        self.assertEqual(frame.payload, b"")

    def test_every_config_pair_has_get_and_set_builder(self):
        for pair in CONFIGURATION_COMMANDS:
            get_frame = parse_normal_report(build_config_get_request(pair.key))
            set_frame = parse_normal_report(build_config_set_request(pair.key.value, b"proof"))
            self.assertEqual(get_frame.command, pair.get)
            self.assertEqual(get_frame.payload, b"")
            self.assertEqual(set_frame.command, pair.set)
            self.assertEqual(set_frame.payload, b"proof")
            self.assertEqual(set_frame.frame_type, NormalFrameType.PAYLOAD_OR_CONTINUATION)

    def test_config_set_preserves_bytes_or_encodes_text(self):
        raw = parse_normal_report(
            build_config_set_request(ConfigurationKey.ADB_ENABLED, b"\xff")
        )
        text = parse_normal_report(
            build_config_set_request(
                ConfigurationKey.ADB_ENABLED,
                "1",
                frame_type=NormalFrameType.FINAL_PAYLOAD,
            )
        )
        self.assertEqual(raw.payload, b"\xff")
        self.assertEqual((text.payload, text.frame_type), (b"1", 2))

    def test_every_literal_upload_target_builder(self):
        for command in LITERAL_UPLOAD_TARGET_COMMANDS:
            frame = parse_normal_report(build_upload_target_request(command, "/tmp/a"))
            self.assertEqual(frame.command, command)
            self.assertEqual(frame.payload, b"/tmp/a")

    def test_every_derived_upload_target_builder(self):
        for command in DERIVED_UPLOAD_TARGET_COMMANDS:
            frame = parse_normal_report(build_upload_target_request(command))
            self.assertEqual(frame.command, command)
            self.assertEqual(frame.payload, b"")
            with self.assertRaisesRegex(ProtocolError, "must not include"):
                build_upload_target_request(command, "/tmp/not-used")

    def test_upload_state_frames(self):
        initialize = parse_normal_report(build_upload_initialize_request())
        data = parse_normal_report(build_upload_data_request(7, b"abc", final=True))
        commit = parse_normal_report(build_upload_commit_request())
        self.assertEqual(initialize.command, 0x3000)
        self.assertEqual((data.command, data.status, data.frame_type, data.payload), (0x3200, 7, 2, b"abc"))
        self.assertEqual(commit.command, 0x3300)

    def test_upload_target_rejects_firmware_shell_hazard(self):
        with self.assertRaisesRegex(ProtocolError, "safe ASCII"):
            build_upload_target_request(NormalCommand.UPLOAD_TARGET_3110, "/tmp/a;reboot")

    def test_adb_upload_command_target_is_exact_and_narrow(self):
        frame = parse_normal_report(build_adb_upload_command_target_request())
        self.assertEqual(frame.command, NormalCommand.UPLOAD_TARGET_3110)
        self.assertEqual(frame.payload, ADB_UPLOAD_COMMAND_TARGET)
        self.assertEqual(
            frame.payload,
            b"/tmp/.cc2flash-adbd-bootstrap;/bin/adbd&",
        )
        self.assertNotIn(b" ", frame.payload)
        self.assertLessEqual(len(frame.payload), 127)

    def test_upgrade_builder_supports_hid_cdc_and_group_wide_values(self):
        hid = parse_normal_report(build_upgrade_request())
        cdc = parse_normal_report(build_upgrade_request(mode=0x010203A0, command=0x4ABC))
        self.assertEqual((hid.command, hid.payload.hex()), (0x4000, "54445055a1030201"))
        self.assertEqual((cdc.command, cdc.payload.hex()), (0x4ABC, "54445055a0030201"))
        with self.assertRaisesRegex(ProtocolError, "0x4xxx"):
            build_upgrade_request(command=0x3000)


class BootCommandBuilderTests(unittest.TestCase):
    def test_all_four_boot_frame_types_are_cataloged(self):
        self.assertEqual(
            {int(entry.frame_type) for entry in BOOT_FRAME_TYPES},
            {1, 2, 3, 5},
        )

    def test_boot_metadata_and_data_builders(self):
        plan = UpdatePlan(0, 512, 640, "00" * 16, 1, BOOT_DATA_SIZE, 1)
        metadata = parse_boot_frame(build_boot_metadata_request(plan), device_response=False)
        data = parse_boot_frame(build_boot_data_request(9, b"abc"), device_response=False)
        self.assertEqual(metadata.frame_type, BootFrameType.HOST_TRANSFER_METADATA)
        self.assertEqual(struct.unpack("<HIHH", metadata.payload), (3060, 640, 1, 0))
        self.assertEqual(data.frame_type, BootFrameType.HOST_TRANSFER_DATA)
        self.assertEqual((struct.unpack_from("<I", data.payload)[0], data.payload[4:]), (9, b"abc"))
        with self.assertRaisesRegex(ProtocolError, "HID maximum"):
            build_boot_data_request(0, b"x" * (BOOT_DATA_SIZE + 1))

    def test_boot_metadata_builder_closes_zero_divisor_bug(self):
        invalid = UpdatePlan(0, 512, 640, "00" * 16, 1, 0, 1)
        with self.assertRaisesRegex(ProtocolError, "packet payload"):
            build_boot_metadata_request(invalid)

    def test_ack_decoder_uses_next_expected_packet(self):
        bare = device_frame(BootFrameType.DEVICE_NEXT_PACKET_ACK, struct.pack("<I", 7))
        hid = b"\x01" + bare.ljust(BOOT_REPORT_SIZE - 1, b"\0")
        self.assertEqual(decode_boot_ack(bare), 7)
        self.assertEqual(decode_boot_ack(hid), 7)

    def test_terminal_decoder_distinguishes_ram_md5_result(self):
        self.assertTrue(
            decode_boot_terminal_status(
                device_frame(BootFrameType.DEVICE_TERMINAL_STATUS, b"\x01")
            )
        )
        self.assertFalse(
            decode_boot_terminal_status(
                device_frame(BootFrameType.DEVICE_TERMINAL_STATUS, b"\x00")
            )
        )
        with self.assertRaisesRegex(ProtocolError, "00 or 01"):
            decode_boot_terminal_status(
                device_frame(BootFrameType.DEVICE_TERMINAL_STATUS, b"\x02")
            )


if __name__ == "__main__":
    unittest.main()

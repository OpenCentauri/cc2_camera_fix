/*
 * Human-readable reconstruction of the CC2 main-U-Boot USB updater.
 *
 * NOT ORIGINAL SOURCE.  Function addresses refer to the U-Boot image linked at
 * 0x80100000 (flash file offset 0x6800).  This file preserves missing checks and
 * ordering bugs for auditability; it is not a safe updater implementation.
 */

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

enum {
    FLASH_SIZE = 0x00800000,
    UPGRADE_WORD_OFFSET = 0x007f8000,
    HID_REPORT_SIZE = 3072,
    HID_REPORT_ID = 1,
    COMMON_OVERHEAD = 7,
    DATA_OVERHEAD = 11,
    IMAGE_HEADER_SIZE = 128
};

enum updater_type {
    HOST_METADATA = 1,
    DEVICE_ACK = 2,
    HOST_DATA = 3,
    DEVICE_TERMINAL = 5
};

typedef enum {
    TRANSPORT_CDC,
    TRANSPORT_HID
} update_transport;

typedef struct {
    uint16_t subpack_size;
    uint32_t transfer_size;
    uint16_t packets_per_ack;
    uint16_t version;
} transfer_metadata;

typedef struct {
    uint8_t ignored0[4];
    uint32_t flash_offset;
    uint32_t image_length;
    uint8_t image_md5[16];
    uint8_t ignored1[100];
} image_header;

typedef struct {
    transfer_metadata meta;
    uint32_t packet_count;
    uint32_t batch_count;
    uint32_t next_packet;
    uint32_t batch_first_packet;
    uint32_t stream_used;
    uint8_t *stream;
    uint8_t bad_frame_count;
    uint8_t stall_count;
    uint8_t terminal_state;
} updater_state;

typedef struct spi_flash spi_flash;

static updater_state U;
static spi_flash *G_flash;
static update_transport G_transport;
static uint8_t G_device_frame[512];
static uint16_t G_device_frame_length;

static uint16_t rd16(const uint8_t *p) {
    return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t rd32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void wr16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void wr32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t ceil_div_u32(uint32_t n, uint32_t d) {
    /* BUG-COMPAT: callers do not establish d != 0. */
    return (n + d - 1) / d;
}

/* Vendor/U-Boot effects outside the protocol state machine. */
extern int transport_send(const uint8_t *frame, size_t frame_length);
extern void uboot_md5(const uint8_t *data, uint32_t length, uint8_t out[16]);
extern spi_flash *spi_flash_probe(unsigned bus, unsigned chip_select,
                                  unsigned hz, unsigned mode);
extern int spi_flash_erase(spi_flash *flash, uint32_t offset, uint32_t length);
extern int spi_flash_write(spi_flash *flash, uint32_t offset, uint32_t length,
                           const void *data);
extern int spi_flash_read(spi_flash *flash, uint32_t offset, uint32_t length,
                          void *data);
extern void mdelay(unsigned milliseconds);

/* 0x80117948 */
static int validate_host_frame(const uint8_t *frame, uint16_t received_length)
{
    static const uint8_t host_magic[3] = {0x80, 0x00, 0xee};
    if (memcmp(frame, host_magic, 3) != 0)
        return -1;

    /* CONFIRMED: validation is driven by the supplied/global received length;
     * bytes 0..length-2 sum modulo 256 and must equal the last byte. */
    uint8_t sum = 0;
    for (uint16_t i = 0; i + 1 < received_length; ++i)
        sum = (uint8_t)(sum + frame[i]);
    return sum == frame[received_length - 1] ? 0 : -1;
}

/* 0x801179ec */
static const uint8_t *build_device_frame(uint8_t type,
                                         const void *payload,
                                         uint16_t payload_length)
{
    memset(G_device_frame, 0, sizeof(G_device_frame));
    G_device_frame[0] = 0x81;
    G_device_frame[1] = 0x00;
    G_device_frame[2] = 0xee;
    G_device_frame[3] = type;
    G_device_frame_length = (uint16_t)(payload_length + COMMON_OVERHEAD);
    wr16(G_device_frame + 4, G_device_frame_length);
    if (payload)
        memcpy(G_device_frame + 6, payload, payload_length);

    uint8_t sum = 0;
    for (uint16_t i = 0; i + 1 < G_device_frame_length; ++i)
        sum = (uint8_t)(sum + G_device_frame[i]);
    G_device_frame[G_device_frame_length - 1] = sum;
    return G_device_frame;
}

static void send_ack(uint32_t next_expected_packet)
{
    uint8_t payload[4];
    wr32(payload, next_expected_packet);
    build_device_frame(DEVICE_ACK, payload, 4);
    (void)transport_send(G_device_frame, G_device_frame_length);
}

static void send_terminal(uint8_t success)
{
    build_device_frame(DEVICE_TERMINAL, &success, 1);
    (void)transport_send(G_device_frame, G_device_frame_length);
}

/*
 * 0x80117b28 — retry/batch helper, represented semantically.
 *
 * A bad frame rewinds to the first packet in the current batch and ACKs that
 * absolute number.  The bad-frame path sends failure on the fifth error.  A
 * separate stalled-transfer counter also fails when it reaches five.
 */
static int request_batch_retry(int stalled)
{
    uint8_t *counter = stalled ? &U.stall_count : &U.bad_frame_count;
    ++*counter;
    if (*counter >= 5) {
        send_terminal(0);
        U.terminal_state = 2;
        return -1;
    }

    U.next_packet = U.batch_first_packet;
    /* stream_used is rewound to the beginning of the current packet batch. */
    U.stream_used = U.batch_first_packet * U.meta.subpack_size;
    send_ack(U.batch_first_packet);
    return 0;
}

/* Metadata body is exactly the ten bytes at common-frame offset 6. */
static void decode_metadata_unchecked(const uint8_t *p, transfer_metadata *m)
{
    m->subpack_size = rd16(p + 0);
    m->transfer_size = rd32(p + 2);
    m->packets_per_ack = rd16(p + 6);
    m->version = rd16(p + 8);
}

/*
 * Type-1 branch within 0x80117d1c.
 */
static int accept_metadata(const uint8_t *frame, uint16_t received_length)
{
    /* Caller already compared only magic and frame[3]. */
    (void)validate_host_frame(frame, received_length);
    /* BUG-COMPAT: the return value above is ignored.  Ten bytes are copied even
     * when total length is not the canonical 17 bytes. */
    decode_metadata_unchecked(frame + 6, &U.meta);

    uint32_t frame_size = (uint32_t)U.meta.subpack_size + DATA_OVERHEAD;
    U.packet_count = ceil_div_u32(U.meta.transfer_size, U.meta.subpack_size);
    uint32_t batch_size = frame_size * U.meta.packets_per_ack;
    (void)batch_size; /* The binary allocates a batch assembly buffer of this size. */
    U.batch_count = ceil_div_u32(U.packet_count, U.meta.packets_per_ack);

    /* BUG-COMPAT: no maximum transfer size; no zero-divisor checks; allocation
     * is based entirely on unauthenticated host metadata. */
    U.stream = malloc(U.meta.transfer_size);
    if (!U.stream)
        return -1;

    U.next_packet = 0;
    U.batch_first_packet = 0;
    U.stream_used = 0;
    U.bad_frame_count = 0;
    U.stall_count = 0;
    U.terminal_state = 0;
    send_ack(0);
    return 0;
}

/* Finalization path within 0x801183b0..0x80118590. */
static int verify_and_program(void)
{
    image_header header;
    uint8_t computed[16];

    /* CONFIRMED: first 128 stream bytes are copied into a stack header. */
    memcpy(&header, U.stream, IMAGE_HEADER_SIZE);

    /* BUG-COMPAT: header.image_length is not checked against transfer_size or
     * received bytes before this read. */
    uboot_md5(U.stream + IMAGE_HEADER_SIZE, header.image_length, computed);
    if (memcmp(computed, header.image_md5, 16) != 0) {
        send_terminal(0);
        U.terminal_state = 2;
        return -1;
    }

    /* Critical ordering: success means RAM MD5 matched, not flash programmed. */
    send_terminal(1);
    mdelay(200);

    /* BUG-COMPAT: there is no check equivalent to
     *   flash_offset + image_length <= 0x00800000.
     * Return values are ignored and there is no readback. */
    (void)spi_flash_erase(G_flash, header.flash_offset, header.image_length);
    (void)spi_flash_write(G_flash, header.flash_offset, header.image_length,
                          U.stream + IMAGE_HEADER_SIZE);
    U.terminal_state = 1;
    return 0;
}

/* Type-3 branch within 0x80118094..0x80118678. */
static int accept_data(const uint8_t *frame, uint16_t received_length)
{
    if (validate_host_frame(frame, received_length) != 0)
        return request_batch_retry(0);

    uint32_t packet = rd32(frame + 6);
    uint16_t declared = rd16(frame + 4);
    uint32_t data_length = (uint32_t)declared - DATA_OVERHEAD;
    if (packet != U.next_packet)
        return request_batch_retry(0);

    /* For a valid producer, every nonfinal packet carries subpack_size bytes.
     * The binary trusts its calculated destination/allocation relationship. */
    memcpy(U.stream + U.stream_used, frame + 10, data_length);
    U.stream_used += data_length;
    ++U.next_packet;
    U.bad_frame_count = 0;

    if (packet == U.packet_count - 1)
        return verify_and_program();

    if ((U.next_packet % U.meta.packets_per_ack) == 0) {
        send_ack(U.next_packet);
        U.batch_first_packet = U.next_packet;
    }
    return 0;
}

/*
 * 0x80117d1c — common updater state-machine entry, used by HID and CDC.
 */
int updater_receive(const uint8_t *frame, uint16_t received_length)
{
    static const uint8_t host_magic[3] = {0x80, 0x00, 0xee};
    (void)received_length;
    /* CONFIRMED: the state machine copies/uses the frame's own u16 length.  It
     * does not establish that this equals the bytes actually supplied by USB. */
    uint16_t declared_length = rd16(frame + 4);
    if (memcmp(frame, host_magic, 3) != 0)
        return 0;

    switch (frame[3]) {
    case HOST_METADATA:
        return accept_metadata(frame, declared_length);
    case HOST_DATA:
        return accept_data(frame, declared_length);
    default:
        /* CONFIRMED: no generic command dispatcher and no response branch for
         * read/query/cancel/reboot/erase operations. */
        return 0;
    }
}

/* 0x801186ac */
int updater_spi_init(void)
{
    G_flash = spi_flash_probe(0, 0, 20000000, 3);
    /* The original prints on failure; normal progress requires non-NULL. */
    return G_flash ? 0 : -1;
}

/* 0x80118714 */
update_transport select_update_transport(void)
{
    uint32_t words[2] = {0, 0};
    (void)spi_flash_read(G_flash, UPGRADE_WORD_OFFSET, 8, words);

    if (words[1] == 0x010203a0)
        G_transport = TRANSPORT_CDC;
    else
        /* CONFIRMED: 0x010203a1 selects HID; other values fall through the same
         * effective/default path in this build. */
        G_transport = TRANSPORT_HID;
    return G_transport;
}

/*
 * 0x8011b9d8 — HID transport envelope, simplified around its observable format.
 */
int hid_receive_report(const uint8_t report[HID_REPORT_SIZE])
{
    if (report[0] != HID_REPORT_ID)
        return -1;
    const uint8_t *embedded = report + 1;
    uint16_t embedded_length = rd16(embedded + 4);
    return updater_receive(embedded, embedded_length);
}

void hid_send_embedded_frame(const uint8_t *frame, uint16_t frame_length,
                             uint8_t report[HID_REPORT_SIZE])
{
    memset(report, 0, HID_REPORT_SIZE);
    report[0] = HID_REPORT_ID;
    memcpy(report + 1, frame, frame_length);
    /* The actual gadget sends all 3,072 bytes.  Padding is not checksummed. */
}

/* CDC supplies the same common frame directly, with no report-ID/padding layer. */
int cdc_receive_frame(const uint8_t *frame, uint16_t frame_length)
{
    return updater_receive(frame, frame_length);
}

/*
 * Negative evidence from exhaustive branch review:
 *
 * - spi_flash_read exists for boot/console use and for the two upgrade words;
 * - no HOST_* type or updater_receive branch returns flash bytes to USB;
 * - no branch verifies data after spi_flash_write;
 * - no signature/authentication routine gates metadata or data.
 */

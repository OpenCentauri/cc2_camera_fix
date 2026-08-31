/*
 * Human-readable reconstruction of Linux /bin/hid_update.
 *
 * NOT ORIGINAL SOURCE.  Address comments refer to the supplied stripped
 * MIPS32r2 ELF.  This file intentionally preserves observable quirks so it can
 * be compared with the binary; do not compile or deploy it as an updater.
 */

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>

enum {
    NORMAL_REPORT_SIZE = 1024,
    NORMAL_PAYLOAD_OFF = 14,
    UPLOAD_CAPACITY = 0x01000200,
    UPGRADE_WORD_OFFSET = 0x007f8000,
    UCAMERA_SET_FLASH = 0x2000550c,
    UCAMERA_GET_FLASH = 0x2000550d,
    UCAMERA_ENABLE_HID = 0x20005508
};

enum normal_status {
    ST_OK = 0,
    ST_FAIL = 1,
    ST_BAD_REPORT_ID = 2,
    ST_BAD_MAGIC = 3,
    ST_BAD_CRC = 4,
    ST_UNKNOWN_GROUP = 5
};

enum upload_state {
    UP_IDLE = 1,
    UP_NEED_TARGET = 2,
    UP_RECEIVING = 3,
    UP_READY = 4,
    UP_ERROR = 5
};

typedef struct {
    int hid_fd;                 /* +0x000 */
    int ucamera_fd;             /* +0x004 */
    uint8_t in[1024];           /* +0x008 */
    uint8_t out[1024];          /* +0x408 */
    uint8_t *upload_data;       /* +0x808 */
    uint32_t upload_capacity;   /* +0x80c */
    uint32_t upload_used;       /* +0x810 */
    uint32_t last_sequence;     /* +0x814 */
    char *destination;          /* +0x818 */
    uint32_t saw_final;         /* +0x81c */
} daemon_context;

typedef struct {
    uint32_t flash_offset;
    uint32_t length;
    void *buffer;
} ucamera_flash_request;

static daemon_context *g_ctx;           /* reconstructed global at 0x00414290 */
static int g_upload_state = UP_IDLE;     /* reconstructed global at 0x00414240 */
static char g_upload_path[128];          /* reconstructed global at 0x004142a0 */

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

/* External effects represented by libc/vendor calls in the binary. */
extern int vendor_flash_ioctl(int fd, unsigned long request,
                              ucamera_flash_request *arg);
extern int write_exact_report(int fd, const void *buf, size_t length);
extern int read_exact_report(int fd, void *buf, size_t length);
extern void reboot_via_shell(void); /* system("reboot") */

/* 0x00400fe8 — CONFIRMED algorithm and initial value. */
static uint16_t cc2_crc16(const uint8_t *data, size_t length, uint16_t crc)
{
    while (length--) {
        crc ^= *data++;
        for (unsigned bit = 0; bit != 8; ++bit) {
            unsigned old_lsb = crc & 1;
            crc >>= 1;
            if (old_lsb)
                crc ^= 0x1021;
        }
    }
    return crc;
}

/*
 * 0x00401034 — response constructor.
 *
 * The original ABI passes payload_length as a fifth stack argument.  Payload is
 * copied only when response_type == 1.  Every write is a full 1,024-byte report.
 */
static int send_normal_response(uint16_t command, uint32_t status,
                                uint8_t response_type,
                                const uint8_t *payload,
                                uint16_t payload_length)
{
    uint8_t *r = g_ctx->out;
    memset(r, 0, NORMAL_REPORT_SIZE);

    r[0] = 1;
    r[1] = 0x5a;
    r[2] = 0x5a;
    wr16(r + 3, command);
    wr32(r + 8, status);

    if (response_type == 1 && payload_length != 0) {
        r[5] = 1;
        wr16(r + 6, payload_length);
        memcpy(r + NORMAL_PAYLOAD_OFF, payload, payload_length);
    }

    uint16_t crc = cc2_crc16(r, 12, 0xffff);
    if (r[5] == 1)
        crc = cc2_crc16(r + NORMAL_PAYLOAD_OFF, payload_length, crc);
    wr16(r + 12, crc);

    return write_exact_report(g_ctx->hid_fd, r, NORMAL_REPORT_SIZE) < 0 ? -1 : 0;
}

typedef struct {
    uint16_t get_command;
    const char *key;
} config_binding;

/* 0x004024c0 — table recovered from the dispatch tree/data table. */
static const config_binding config_bindings[] = {
    {0x2000, "manufact_lab"}, {0x2010, "serial_lab"},
    {0x2020, "productnumber"}, {0x2030, "vendor_id"},
    {0x2040, "product_id"}, {0x2050, "device_bcd"},
    {0x2f00, "model_lab"}, {0x2f10, "cmei_lab"},
    {0x2f20, "appkey"}, {0x2f30, "product_lab"},
    {0x2f40, "adb_en"}, {0x2f50, "sensor_name"},
    {0x2f60, "i2c_addr"}, {0x2f70, "default_boot"},
    {0x2f80, "sensor_fps"}, {0x2f90, "sensor_width"},
    {0x2fa0, "sensor_height"}, {0x2fb0, "hvflip"},
    {0x2fc0, "rcmode"}, {0x2fd0, "bitrate"},
    {0x2fe0, "qp_value"}
};

static const char *config_key_for(uint16_t command, int *is_set)
{
    for (size_t i = 0; i != sizeof(config_bindings) / sizeof(config_bindings[0]); ++i) {
        if (command == config_bindings[i].get_command) {
            *is_set = 0;
            return config_bindings[i].key;
        }
        if (command == (uint16_t)(config_bindings[i].get_command + 1)) {
            *is_set = 1;
            return config_bindings[i].key;
        }
    }
    return NULL;
}

/*
 * 0x00401cd0 — configuration GET, normalized into readable C.
 * The binary uses fscanf/sscanf, small fixed buffers, and returns raw bytes
 * after ':' up to the newline.  Missing keys and parse/open errors fail.
 */
static int config_get(uint16_t command, const char *key)
{
    FILE *fp = fopen("/etc/conf.d/uvc.config", "rb+");
    char line[128], parsed_key[64], value[64];
    if (!fp)
        return send_normal_response(command, ST_FAIL, 0, NULL, 0);

    int found = 0;
    while (fgets(line, sizeof(line), fp)) {
        char *colon = strchr(line, ':');
        if (!colon)
            continue;
        *colon = '\0';
        /* INFERRED spelling of the binary's whitespace trimming. */
        if (sscanf(line, "%63s", parsed_key) != 1 || strcmp(parsed_key, key) != 0)
            continue;
        strncpy(value, colon + 1, sizeof(value) - 1);
        value[sizeof(value) - 1] = '\0';
        char *nl = strchr(value, '\n');
        if (nl)
            *nl = '\0';
        found = 1;
        break;
    }
    fclose(fp);

    if (!found)
        return send_normal_response(command, ST_FAIL, 0, NULL, 0);
    return send_normal_response(command, ST_OK, 1,
                                (const uint8_t *)value,
                                (uint16_t)strlen(value));
}

/*
 * 0x00402038 — configuration SET, expressed semantically.
 *
 * CONFIRMED: the original finds the matching line, buffers the remainder of
 * the file, seeks back to the start of the line, writes "<left-side>:<value>",
 * then writes the buffered tail.  If the resulting file position is below the
 * old file size, it writes zero bytes to reach the old size.  It performs no
 * type/range validation and uses fixed stack buffers.
 */
static int config_set(uint16_t command, const char *key,
                      const uint8_t *new_value, uint16_t new_length)
{
    FILE *fp = fopen("/etc/conf.d/uvc.config", "rb+");
    char original[2048] = {0};
    char rewritten[4096] = {0}; /* readable stand-in for several stack buffers */
    if (!fp)
        return send_normal_response(command, ST_FAIL, 0, NULL, 0);

    fseek(fp, 0, SEEK_END);
    long old_size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    /* BUG-COMPAT: the binary's corresponding tail buffer is only 2,048 bytes. */
    fread(original, 1, (size_t)old_size, fp);

    char *cursor = original;
    char *file_end = original + old_size;
    int found = 0;
    while (cursor < file_end) {
        char *line_end = memchr(cursor, '\n', (size_t)(file_end - cursor));
        if (!line_end)
            line_end = file_end;
        char *colon = memchr(cursor, ':', (size_t)(line_end - cursor));
        if (colon) {
            char parsed_key[96] = {0};
            size_t key_length = (size_t)(colon - cursor);
            if (key_length >= sizeof(parsed_key))
                key_length = sizeof(parsed_key) - 1;
            memcpy(parsed_key, cursor, key_length);
            char *space = strchr(parsed_key, ' ');
            if (space)
                *space = '\0';

            if (strcmp(parsed_key, key) == 0) {
                size_t prefix_length = (size_t)(colon - original + 1);
                size_t tail_offset = (size_t)(line_end - original);
                size_t tail_length = (size_t)(file_end - line_end);
                memcpy(rewritten, original, prefix_length);
                memcpy(rewritten + prefix_length, new_value, new_length);
                memcpy(rewritten + prefix_length + new_length,
                       original + tail_offset, tail_length);

                size_t new_size = prefix_length + new_length + tail_length;
                if (new_size < (size_t)old_size) {
                    memset(rewritten + new_size, 0, (size_t)old_size - new_size);
                    new_size = (size_t)old_size;
                }
                fseek(fp, 0, SEEK_SET);
                fwrite(rewritten, 1, new_size, fp);
                found = 1;
                break;
            }
        }
        cursor = line_end < file_end ? line_end + 1 : file_end;
    }
    fclose(fp);
    return send_normal_response(command, found ? ST_OK : ST_FAIL, 0, NULL, 0);
}

/* 0x004024c0 — returns -1 without replying for an unknown 0x2xxx command. */
static int dispatch_config(const uint8_t *r)
{
    uint16_t command = rd16(r + 3);
    int is_set = 0;
    const char *key = config_key_for(command, &is_set);
    if (!key)
        return -1;
    if (is_set)
        return config_set(command, key, r + 14, rd16(r + 6));
    return config_get(command, key);
}

/* 0x00402830 */
static int upload_init(daemon_context *ctx)
{
    if (!ctx->upload_data)
        ctx->upload_data = malloc(UPLOAD_CAPACITY);
    if (!ctx->upload_data)
        return -1;

    ctx->upload_capacity = UPLOAD_CAPACITY;
    ctx->upload_used = 0;
    ctx->last_sequence = 0;
    ctx->destination = NULL;
    ctx->saw_final = 0;
    return 0;
}

/* 0x004028dc */
static int upload_append(const uint8_t *r, daemon_context *ctx)
{
    uint32_t sequence = rd32(r + 8);
    uint16_t length = rd16(r + 6);

    if (sequence >= 2 && sequence != ctx->last_sequence + 1)
        return -1;
    if (ctx->upload_used + length > ctx->upload_capacity)
        return -1;

    memcpy(ctx->upload_data + ctx->upload_used, r + 14, length);
    if (r[5] == 2) {
        ctx->saw_final = 1;
        g_upload_state = UP_READY;
    }
    ctx->upload_used += length;
    ctx->last_sequence = sequence;
    return 0;
}

/* 0x004029f0 */
static int upload_commit(daemon_context *ctx)
{
    char shell[256];
    FILE *fp;

    /* BUG-COMPAT: destination is interpolated into unquoted shell commands. */
    sprintf(shell, "rm %s", ctx->destination);
    if (ctx->saw_final != 1)
        return -1;
    system(shell);
    system("sync");

    fp = fopen(ctx->destination, "wb");
    if (!fp)
        return -1;
    if (fwrite(ctx->upload_data, 1, ctx->upload_used, fp) != ctx->upload_used) {
        fclose(fp);
        return -1;
    }

    sprintf(shell, "chmod 0777 %s", ctx->destination);
    system(shell);
    fclose(fp);

    if (strcmp(ctx->destination, "/system/bin/hid_update") == 0) {
        /* The binary constructs "cp %s %s", then strangely executes the
         * literal string "command", followed by sync. */
        sprintf(shell, "cp %s %s", "/system/bin/hid_update",
                "/system/bin/hid_update_bak");
        system("command");
        system("sync");
    }

    free(ctx->upload_data);
    ctx->upload_data = NULL;
    return 0;
}

/* 0x00402ba8 */
static int build_sensor_path(int which, char out[128])
{
    FILE *fp = fopen("/etc/conf.d/uvc.config", "r");
    char line[256], sensor[64] = {0};
    if (!fp)
        return -1;
    while (fgets(line, sizeof(line), fp)) {
        if (sscanf(line, "sensor_name : %63s", sensor) == 1)
            break;
    }
    fclose(fp);
    if (!sensor[0])
        return -1;

    if (which == 1)
        sprintf(out, "/system/lib/modules/sensor_%s_t31.ko", sensor);
    else if (which == 2)
        sprintf(out, "/system/etc/sensor/%s-t31.bin", sensor);
    else
        return -1;
    return 0;
}

static int command_uses_literal_path(uint16_t c)
{
    switch (c) {
    case 0x3110: case 0x3111: case 0x3112: case 0x3113:
    case 0x3116: case 0x3120: case 0x3150: case 0x31f0:
        return 1;
    default:
        return 0;
    }
}

/*
 * 0x00402e3c — full 0x3xxx dispatch.
 * Unknown commands return without a response.
 */
static int dispatch_upload(const uint8_t *r, daemon_context *ctx)
{
    uint16_t command = rd16(r + 3);
    int rc;

    if (command == 0x3000) {
        if (g_upload_state != UP_IDLE)
            return send_normal_response(command, ST_OK, 0, NULL, 0);
        rc = upload_init(ctx);
        if (rc < 0) {
            /* CONFIRMED: unlike packet/commit failure, allocation failure does
             * not store state 5; the state remains idle. */
            return send_normal_response(command, ST_FAIL, 0, NULL, 0);
        }
        g_upload_state = UP_NEED_TARGET;
        return send_normal_response(command, ST_OK, 0, NULL, 0);
    }

    if (command_uses_literal_path(command)) {
        if (g_upload_state != UP_NEED_TARGET)
            return send_normal_response(command, ST_OK, 0, NULL, 0);
        memset(g_upload_path, 0, sizeof(g_upload_path));
        /* BUG-COMPAT: binary copies declared length without a 128-byte bound. */
        memcpy(g_upload_path, r + 14, rd16(r + 6));
        char *space = strchr(g_upload_path, ' ');
        if (space)
            *space = '\0';
        ctx->destination = g_upload_path;
        g_upload_state = UP_RECEIVING;
        return send_normal_response(command, ST_OK, 0, NULL, 0);
    }

    if (command == 0x3114 || command == 0x3115) {
        if (g_upload_state != UP_NEED_TARGET)
            return send_normal_response(command, ST_OK, 0, NULL, 0);
        rc = build_sensor_path(command == 0x3114 ? 1 : 2, g_upload_path);
        if (rc < 0)
            return send_normal_response(command, ST_FAIL, 0, NULL, 0);
        ctx->destination = g_upload_path;
        g_upload_state = UP_RECEIVING;
        return send_normal_response(command, ST_OK, 0, NULL, 0);
    }

    if (command == 0x3200) {
        /* BUG-COMPAT: compiled ((state-3) & -3) == 0 accepts 3 and 5. */
        if (!(g_upload_state == UP_RECEIVING || g_upload_state == UP_ERROR))
            return send_normal_response(command, ST_OK, 0, NULL, 0);
        rc = upload_append(r, ctx);
        if (rc < 0) {
            g_upload_state = UP_ERROR;
            return send_normal_response(command, ST_FAIL, 0, NULL, 0);
        }
        return send_normal_response(command, ST_OK, 0, NULL, 0);
    }

    if (command == 0x3300) {
        /* Same flawed state test: state 4 and state 5 reach commit. */
        if (!(g_upload_state == UP_READY || g_upload_state == UP_ERROR))
            return send_normal_response(command, ST_OK, 0, NULL, 0);
        rc = upload_commit(ctx);
        if (rc < 0) {
            g_upload_state = UP_ERROR;
            free(ctx->upload_data);
            ctx->upload_data = NULL;
            return send_normal_response(command, ST_FAIL, 0, NULL, 0);
        }
        g_upload_state = UP_IDLE;
        return send_normal_response(command, ST_OK, 0, NULL, 0);
    }

    return -1;
}

/* 0x00401364 within dispatch_normal_report. */
static int set_upgrade_words_and_reboot(uint16_t command,
                                        const uint8_t *payload,
                                        uint16_t length)
{
    uint8_t *heap_copy = malloc(length);
    uint32_t readback[2] = {0, 0};
    ucamera_flash_request req;
    /* BUG-COMPAT: allocation is not checked before the copy. */
    memcpy(heap_copy, payload, length);

    /* A normal HID trigger supplies these eight bytes:
     *   54 44 50 55 a1 03 02 01
     * which become words 0x55504454 and 0x010203a1.  The binary itself does
     * not verify that value or require command 0x4000. */

    req.flash_offset = UPGRADE_WORD_OFFSET;
    req.length = 8;
    req.buffer = heap_copy;
    int rc = vendor_flash_ioctl(g_ctx->ucamera_fd, UCAMERA_SET_FLASH, &req);
    if (rc != 0)
        (void)send_normal_response(command, ST_FAIL, 0, NULL, 0);
    else {
        req.buffer = readback;
        (void)vendor_flash_ioctl(g_ctx->ucamera_fd, UCAMERA_GET_FLASH, &req);
        /* CONFIRMED: readback is logged, not compared with requested words. */
        (void)send_normal_response(command, ST_OK, 0, NULL, 0);
    }
    reboot_via_shell();
    free(heap_copy);
    return 0;
}

/* 0x004015e4 within the top-level dispatcher. */
static int version_query(uint16_t command)
{
    FILE *fp = fopen("/tmp/version.txt", "r+");
    char line[24] = {0};
    if (!fp)
        return send_normal_response(command, ST_FAIL, 0, NULL, 0);
    if (fscanf(fp, "%23[^\n]", line) < 0) {
        fclose(fp);
        return send_normal_response(command, ST_FAIL, 0, NULL, 0);
    }
    fclose(fp);
    return send_normal_response(command, ST_OK, 1,
                                (const uint8_t *)line,
                                (uint16_t)strlen(line));
}

/*
 * 0x0040123c — normal report parser and top-level command dispatcher.
 */
static int dispatch_normal_report(const uint8_t *r, size_t received)
{
    (void)received; /* CONFIRMED: handler normally supplies exactly 1,024. */

    uint16_t command = rd16(r + 3);
    if (r[0] != 1)
        return send_normal_response(command, ST_BAD_REPORT_ID, 0, NULL, 0);
    if (r[1] != 0x5a || r[2] != 0x5a)
        return send_normal_response(command, ST_BAD_MAGIC, 0, NULL, 0);

    uint16_t computed = cc2_crc16(r, 12, 0xffff);
    if (r[5] == 1 || r[5] == 2) {
        /* BUG-COMPAT: declared length is not bounded to the input report. */
        computed = cc2_crc16(r + 14, rd16(r + 6), computed);
    }
    if (computed != rd16(r + 12))
        return send_normal_response(command, ST_BAD_CRC, 0, NULL, 0);

    switch (command & 0xf000) {
    case 0x0000:
        return command == 0x0001 ? version_query(command) : 0; /* no response */
    case 0x1000:
        return 0; /* recognized no-op group, no response */
    case 0x2000:
        return dispatch_config(r);
    case 0x3000:
        return dispatch_upload(r, g_ctx);
    case 0x4000:
        /* CONFIRMED: every 0x4xxx command, not just 0x4000, reaches here. */
        return set_upgrade_words_and_reboot(command, r + 14, rd16(r + 6));
    default:
        return send_normal_response(command, ST_UNKNOWN_GROUP, 0, NULL, 0);
    }
}

/*
 * 0x00401b94 — separate helper.  It is documented because it clarifies ioctl
 * 0x20005508, though no call from the reconstructed C main was found.
 */
static int probe_ucamera_ready(void)
{
    int fd = /* open("/dev/ucamera", O_RDWR, 0666) */ -1;
    int value = -1;
    if (fd < 0 || read_exact_report(fd, &value, 4) < 0)
        return -1;
    if (value != 0)
        return 0;
    (void)vendor_flash_ioctl(fd, UCAMERA_ENABLE_HID, NULL);
    if (read_exact_report(fd, &value, 4) < 0)
        return -1;
    return value == 1 ? 0 : -1;
}

/*
 * 0x004017d4 — HID event thread, condensed around the protocol behavior.
 */
static void *hid_event_thread(void *unused)
{
    (void)unused;
    /* prctl(PR_SET_NAME, "hid_event"); */
    g_ctx = calloc(2080, 1);
    if (!g_ctx)
        return NULL;

    g_ctx->hid_fd = /* open("/dev/hidg0", O_RDWR, 0666) */ -1;
    g_ctx->ucamera_fd = /* open("/dev/ucamera", O_RDWR, 0666) */ -1;

    uint32_t flags[2] = {0, 0};
    ucamera_flash_request req = {UPGRADE_WORD_OFFSET, 8, flags};
    if (g_ctx->ucamera_fd >= 0) {
        (void)vendor_flash_ioctl(g_ctx->ucamera_fd, UCAMERA_GET_FLASH, &req);
        if (flags[0] == 0x55504454) {
            flags[0] = 0xffffffff;
            flags[1] = 0xffffffff;
            (void)vendor_flash_ioctl(g_ctx->ucamera_fd, UCAMERA_SET_FLASH, &req);
        }
    }

    for (;;) {
        /* The binary builds fd_set/timeval manually, selects with a five-second
         * timeout, tolerates EINTR, and reads until all 1,024 bytes arrive. */
        if (g_ctx->hid_fd < 0)
            continue;
        if (read_exact_report(g_ctx->hid_fd, g_ctx->in, NORMAL_REPORT_SIZE) < 0)
            continue;
        (void)dispatch_normal_report(g_ctx->in, NORMAL_REPORT_SIZE);
    }
}

/* 0x00400e90 — process-watch thread. */
static void *process_watch_thread(void *unused)
{
    (void)unused;
    for (;;) {
        char line[128] = {0};
        system("ps > /tmp/1.txt");
        FILE *fp = fopen("/tmp/1.txt", "r");
        if (fp) {
            while (!feof(fp)) {
                /* Original uses fscanf and compares seven bytes extracted from
                 * a fixed column with "ucamera".  It merely stops scanning. */
                if (fscanf(fp, "%127[^\n]\n", line) < 0)
                    break;
                if (strlen(line) >= 33 && strncmp(line + 26, "ucamera", 7) == 0)
                    break;
            }
            fclose(fp);
        }
        sleep(15);
    }
    return NULL; /* unreachable; keeps the reconstructed pthread signature tidy */
}

/* 0x00400b90 — actual C main; ELF CRT entry is at 0x00400c90. */
int linux_main(void)
{
    pthread_t hid_thread, watch_thread;
    pthread_attr_t attr;

    /* sem_init(global_sem, 0, 0); */
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_attr_setschedpolicy(&attr, 0);
    (void)pthread_create(&hid_thread, &attr, hid_event_thread, NULL);

    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_attr_setschedpolicy(&attr, 0);
    (void)pthread_create(&watch_thread, &attr, process_watch_thread, NULL);

    for (;;)
        sleep(60);
}

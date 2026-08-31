/*
 * Human-readable reconstruction of the SPL upgrade-mode decision.
 * NOT ORIGINAL SOURCE.
 */

#include <stdint.h>

enum {
    UPGRADE_WORD_OFFSET = 0x007f8000,
    UPGRADE_MAGIC = 0x55504454,
    MODE_CDC = 0x010203a0,
    MODE_HID = 0x010203a1
};

extern int spl_spi_read(uint32_t offset, uint32_t length, void *destination);
extern void boot_main_uboot_updater(void);
extern void continue_normal_boot(void);

/* Flash read anchored at 0xf0001c28; comparison anchored at 0xf0001c54. */
void spl_choose_boot_path(void)
{
    uint32_t words[2] = {0, 0};
    (void)spl_spi_read(UPGRADE_WORD_OFFSET, 8, words);

    if (words[0] == UPGRADE_MAGIC) {
        /* SPL only gates on word 0.  Main U-Boot later interprets word 1 as
         * 0x010203a0 for CDC or 0x010203a1 for HID/default. */
        boot_main_uboot_updater();
    } else {
        continue_normal_boot();
    }
}

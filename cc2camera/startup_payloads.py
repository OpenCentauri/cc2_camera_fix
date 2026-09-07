"""Independently written, stock BusyBox/Hush-compatible boot hook payloads."""
from .restore_prepare import INSTRUCTIONS, SYMBOLS
from .adb_backup import EXPECTED_PARTITIONS

# These v1 script bytes are an installed-file contract: startup.py recognizes
# only exact contents. Keep their cc2flash markers and log prefixes stable so
# existing installations remain idempotent and can add the other managed hook.
RUNNER = b'''#!/bin/sh
# cc2flash hook runner v1
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
LC_ALL=C
export LC_ALL
if [ "$1" != --ram-worker ]; then
    # The config script descriptor must close across exec before any unmount.
    busybox awk '$2=="/tmp" && $3=="tmpfs" {ok++} END {exit(ok!=1)}' /proc/mounts || exit 1
    umask 077
    stage=$(busybox mktemp -d /tmp/cc2-hooks.XXXXXX) || exit 1
    mkdir "$stage/enabled" || exit 1
    cp /etc/conf.d/system.sh "$stage/runner" || exit 1
    for hook in /etc/conf.d/enabled/*; do
        if [ ! -e "$hook" ] && [ ! -L "$hook" ]; then continue; fi
        [ -f "$hook" ] && [ ! -L "$hook" ] || exit 1
        cp "$hook" "$stage/enabled/" || exit 1
    done
    cd / || exit 1
    exec /bin/sh "$stage/runner" --ram-worker "$stage" </dev/null >>/tmp/cc2-hooks.log 2>&1
    exit 1
fi
CC2_STAGE=$2
export CC2_STAGE
for hook in "$CC2_STAGE"/enabled/*; do
    [ -f "$hook" ] || continue
    echo "cc2flash: running ${hook##*/}"
    /bin/sh "$hook"
    result=$?
    echo "cc2flash: ${hook##*/} exit $result"
    # Never release rcS into camera startup with config missing or unusable.
    if ! busybox awk '$2=="/etc/conf.d" {n++; if($1!="/dev/mtdblock5" || $3!="jffs2" || $4 !~ /(^|,)rw(,|$)/)bad=1} END {exit(n!=1 || bad)}' /proc/mounts; then
        echo 'cc2flash: CONFIG UNUSABLE; boot held. Recover via ADB; do not write config.'
        /bin/adbd &
        while :; do sleep 60; done
    fi
done
rm -rf "$CC2_STAGE"
'''

ADB_HOOK = b'''#!/bin/sh
# cc2flash adb hook v1
busybox pidof adbd >/dev/null || /bin/adbd &
'''

# Boot compatibility fingerprint; the installer also checks full SHA-256.
KERNEL_MD5 = "388e256470b2ad70f4a29cc37e0fee32"


def erase_hook() -> bytes:
    """Build the exact-kernel-gated, early-boot-only RAM correction."""
    checks = '\n'.join(f'[ "$(word {a:#x})" = "0x{v:08X}" ] || fail instructions' for a,v in INSTRUCTIONS.items())
    symbols = '\n'.join(f'[ "$(busybox awk \'$3=="{n}" {{print $1}}\' /proc/kallsyms)" = "{a:08x}" ] || fail symbols' for n,a in SYMBOLS.items())
    mtd = '\n'.join(f'mtd{i}: {size:08x} 00004000 "{name}"' for i,(name,size) in enumerate(EXPECTED_PARTITIONS))
    return ('''#!/bin/sh
# cc2flash erase hook v1; all diagnostics stay in RAM.
fail() { echo "cc2flash: erase fix refused: $*"; exit 1; }
word() { busybox devmem "$1" 32; }
mounted() { busybox awk '$2=="/etc/conf.d" {n++; if($1!="/dev/mtdblock5" || $3!="jffs2" || $4 !~ /(^|,)rw(,|$)/)bad=1} END {exit(n!=1 || bad)}' /proc/mounts; }
[ -n "$CC2_STAGE" ] && [ "$PWD" = / ] || fail runner
busybox pidof ucamera >/dev/null && fail ucamera-running
mounted || fail config-mount
# No other mounts of this filesystem or mounts beneath config.
[ "$(busybox awk '$1=="/dev/mtdblock5" || $2 ~ /^\\/etc\\/conf.d\\// {n++} END {print n+0}' /proc/mounts)" = 1 ] || fail mount-topology
[ "$(busybox sed '1d;y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' /proc/mtd)" = '@MTD@' ] || fail partition-map
@SYMBOLS@
@CHECKS@
# Stock BusyBox MD5 is a build fingerprint, not cryptographic authentication.
hash=$(busybox md5sum /dev/mtd1) || fail kernel-read
[ "$hash" = '@MD5@  /dev/mtd1' ] || fail kernel-hash
pointer=$(word 0x0043b190) || fail pointer
case "$pointer" in 0x????????) ;; *) fail pointer-format;; esac
p=$((pointer))
[ "$p" -ge $((0x80450000)) ] && [ "$p" -le $((0x83fffd9c)) ] && [ "$((p % 4))" -eq 0 ] || fail pointer-range
field=$((p - 0x80000000 + 16))
old=$(word "$field") || fail field
case "$old" in 0x00004000|0x00001000) ;; *) fail erase-size;; esac
# Repeat after potentially lengthy hashing. Never remount under ucamera.
busybox pidof ucamera >/dev/null && fail ucamera-running
sync || fail sync
busybox umount /etc/conf.d || fail unmount
# Arrange ordinary recovery mount on every failure after unmount.
trap 'busybox mount -t jffs2 /dev/mtdblock5 /etc/conf.d || echo "cc2flash: recovery mount FAILED"' 0
busybox awk '$2=="/etc/conf.d" {bad=1} END {exit bad}' /proc/mounts || fail still-mounted
[ "$(word 0x0043b190)" = "$pointer" ] && [ "$(word "$field")" = "$old" ] || fail pointer-changed
@CHECKS@
if [ "$old" != 0x00001000 ]; then
    busybox devmem "$field" 32 0x00001000 || fail ram-write
fi
[ "$(word 0x0043b190)" = "$pointer" ] && [ "$(word "$field")" = 0x00001000 ] || fail readback
busybox mount -t jffs2 /dev/mtdblock5 /etc/conf.d || fail remount
trap - 0
mounted || fail mount-readback
[ "$(busybox sed '1d;y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' /proc/mtd)" = '@MTD@' ] || fail geometry-changed
[ -s /etc/conf.d/serial.cfg ] || fail serial-missing
echo 'cc2flash: erase fix active; master=0x1000; partition geometry=0x4000'
'''.replace('@MTD@',mtd).replace('@SYMBOLS@',symbols).replace('@CHECKS@',checks).replace('@MD5@',KERNEL_MD5)).encode('ascii')

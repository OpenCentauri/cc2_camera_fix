#!/bin/sh
# Independently written camera-side installer. Generated payloads are canonical.
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
LC_ALL=C
export LC_ALL
TOKEN=@TOKEN@
MODE=@MODE@
SELF=/tmp/.cc2-hid-$TOKEN.sh
STAGE=/tmp/.cc2-hid-$TOKEN
CONFIG=/etc/conf.d
STATUS=FAIL

fail() {
    code=00
    case "$1" in
        chmod) code=01;;
        config-changing) code=02;;
        config-mount) code=03;;
        config-path) code=04;;
        config-read) code=05;;
        copy) code=06;;
        destination-appeared) code=07;;
        enabled-path) code=08;;
        final-missing) code=09;;
        fix-inactive) code=10;;
        hid-fingerprint) code=11;;
        identity-file) code=12;;
        incomplete-install) code=13;;
        instructions) code=14;;
        kernel-fingerprint) code=15;;
        managed-link) code=16;;
        missing-hooks) code=17;;
        mkdir) code=18;;
        mode) code=19;;
        mount-topology) code=20;;
        partition-map) code=21;;
        pointer) code=22;;
        pointer-changed) code=23;;
        pointer-format) code=24;;
        pointer-range) code=25;;
        rename) code=26;;
        root) code=27;;
        staged-fix) code=28;;
        staged-runner) code=29;;
        symbols) code=30;;
        sync) code=31;;
        temporary-readback) code=32;;
        unknown-managed-file) code=33;;
        unrelated-hook-type) code=34;;
        starter-type) code=35;;
        starter-stat) code=36;;
        starter-mode) code=37;;
        starter-content) code=38;;
        starter-compare) code=39;;
        erase-hook-type) code=40;;
        erase-hook-stat) code=41;;
        erase-hook-mode) code=42;;
        erase-hook-content) code=43;;
        erase-hook-compare) code=44;;
    esac
    # Preserve the distinction between refusal and a partially written install.
    case "$STATUS" in PART) STATUS=P$code;; *) STATUS=F$code;; esac
    echo "cc2camera-hid: $*"
    exit 1
}
mounted() {
    busybox awk '$2=="/etc/conf.d" {n++; if($1!="/dev/mtdblock5" || $3!="jffs2" || $4 !~ /(^|,)rw(,|$)/)bad=1} END {exit(n!=1 || bad)}' /proc/mounts
}
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
prepare_status() {
    # Missing metadata is supported; links and other file types are not.
    [ ! -L /tmp/version.txt ] || return 1
    if [ -e /tmp/version.txt ]; then
        regular /tmp/version.txt || return 1
        [ "$(busybox stat -c %s /tmp/version.txt)" -le 4096 ] || return 1
        cp -p /tmp/version.txt "$STAGE/version" || return 1
    fi
}
publish_status() {
    [ ! -L /tmp/version.txt ] || return 1
    [ ! -e /tmp/version.txt ] || regular /tmp/version.txt || return 1
    # Rename a complete record so a query cannot see a truncated status.
    printf '%s:%s\n' "$TOKEN" "$STATUS" > "$STAGE/status" || return 1
    cp "$STAGE/status" "$STAGE/status-next" || return 1
    mv "$STAGE/status-next" /tmp/version.txt
}
finish() {
    publish_status || return 1
    sleep 120
    # Leave unexpected replacements and staging diagnostics untouched.
    regular /tmp/version.txt && busybox cmp -s "$STAGE/status" /tmp/version.txt || return 1
    if [ -e "$STAGE/version" ]; then
        mv "$STAGE/version" /tmp/version.txt || return 1
    else
        rm /tmp/version.txt || return 1
    fi
    rm -rf "$STAGE"
    rm -f "$SELF"
}
preflight() {
    [ "$(id -u)" = 0 ] || fail root
    [ ! -L /etc ] && [ ! -L "$CONFIG" ] || fail config-path
    mounted || fail config-mount
    [ "$(busybox awk '$1=="/dev/mtdblock5" || $2 ~ /^\/etc\/conf.d\// {n++} END {print n+0}' /proc/mounts)" = 1 ] || fail mount-topology
    [ "$(busybox sed '1d;y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' /proc/mtd)" = 'mtd0: 00040000 00004000 "boot"
mtd1: 00150000 00004000 "kernel"
mtd2: 00158000 00004000 "root"
mtd3: 004e8000 00004000 "system"
mtd4: 00010000 00004000 "hwconfig"
mtd5: 00020000 00004000 "config"' ] || fail partition-map
    [ "$(busybox md5sum /dev/mtd1)" = '388e256470b2ad70f4a29cc37e0fee32  /dev/mtd1' ] || fail kernel-fingerprint
    [ "$(busybox md5sum /bin/hid_update)" = '8091751fdd4d0d50ea31901663797a86  /bin/hid_update' ] || fail hid-fingerprint
    regular "$CONFIG/serial.cfg" && [ -s "$CONFIG/serial.cfg" ] || fail identity-file
    [ ! -L "$CONFIG/enabled" ] && { [ ! -e "$CONFIG/enabled" ] || [ -d "$CONFIG/enabled" ]; } || fail enabled-path
    for hook in "$CONFIG"/enabled/*; do
        if [ ! -e "$hook" ] && [ ! -L "$hook" ]; then continue; fi
        regular "$hook" || fail unrelated-hook-type
    done
}
managed_failure() {
    case "$dest:$1" in
        "$CONFIG/system.sh:type") fail starter-type;;
        "$CONFIG/system.sh:stat") fail starter-stat;;
        "$CONFIG/system.sh:mode") fail starter-mode;;
        "$CONFIG/system.sh:content") fail starter-content;;
        "$CONFIG/system.sh:compare") fail starter-compare;;
        "$CONFIG/enabled/10-erase-fix.sh:type") fail erase-hook-type;;
        "$CONFIG/enabled/10-erase-fix.sh:stat") fail erase-hook-stat;;
        "$CONFIG/enabled/10-erase-fix.sh:mode") fail erase-hook-mode;;
        "$CONFIG/enabled/10-erase-fix.sh:content") fail erase-hook-content;;
        "$CONFIG/enabled/10-erase-fix.sh:compare") fail erase-hook-compare;;
        *) fail unknown-managed-file;;
    esac
}
known_or_absent() {
    dest=$1
    source=$2
    [ ! -L "$dest" ] || fail managed-link
    if [ -e "$dest" ]; then
        regular "$dest" || managed_failure type
        permissions=$(busybox stat -c %a "$dest") || managed_failure stat
        [ "$permissions" = 755 ] || managed_failure mode
        busybox cmp -s "$source" "$dest"
        comparison=$?
        case "$comparison" in
            0) ;;
            1) managed_failure content;;
            *) managed_failure compare;;
        esac
    fi
}
install_files() {
    # Validate both destinations before the first persistent write.
    known_or_absent "$CONFIG/enabled/10-erase-fix.sh" "$STAGE/fix"
    known_or_absent "$CONFIG/system.sh" "$STAGE/runner"
    if [ -e "$CONFIG/enabled/10-erase-fix.sh" ] && [ -e "$CONFIG/system.sh" ]; then
        STATUS=SAME
        return
    fi
    for n in fix runner; do
        [ ! -e "$CONFIG/.cc2-hid-$n" ] && [ ! -L "$CONFIG/.cc2-hid-$n" ] || fail incomplete-install
    done
    # This is a stability check, NOT a backup or clean-space admission check.
    baseline=$(busybox md5sum /dev/mtd5) || fail config-read
    for n in 1 2; do
        [ "$(busybox md5sum /dev/mtd5)" = "$baseline" ] || fail config-changing
    done
    preflight
    known_or_absent "$CONFIG/enabled/10-erase-fix.sh" "$STAGE/fix"
    known_or_absent "$CONFIG/system.sh" "$STAGE/runner"
    [ "$(busybox md5sum /dev/mtd5)" = "$baseline" ] || fail config-changing
    STATUS=PART
    mkdir -p "$CONFIG/enabled" || fail mkdir
    # Feature first, boot entry point last. Never overwrite an existing hook.
    for n in fix runner; do
        case "$n" in
            fix) dest=$CONFIG/enabled/10-erase-fix.sh;;
            runner) dest=$CONFIG/system.sh;;
        esac
        known_or_absent "$dest" "$STAGE/$n"
        if [ -e "$dest" ]; then continue; fi
        temp=$CONFIG/.cc2-hid-$n
        [ ! -e "$temp" ] && [ ! -L "$temp" ] || fail incomplete-install
        cp "$STAGE/$n" "$temp" || fail copy
        chmod 755 "$temp" || fail chmod
        busybox cmp -s "$STAGE/$n" "$temp" || fail temporary-readback
        sync || fail sync
        [ ! -e "$dest" ] && [ ! -L "$dest" ] || fail destination-appeared
        mv "$temp" "$dest" || fail rename
        sync || fail sync
    done
    known_or_absent "$CONFIG/enabled/10-erase-fix.sh" "$STAGE/fix"
    known_or_absent "$CONFIG/system.sh" "$STAGE/runner"
    regular "$CONFIG/enabled/10-erase-fix.sh" && regular "$CONFIG/system.sh" || fail final-missing
    STATUS=DONE
}
verify_live() {
    known_or_absent "$CONFIG/enabled/10-erase-fix.sh" "$STAGE/fix"
    known_or_absent "$CONFIG/system.sh" "$STAGE/runner"
    regular "$CONFIG/enabled/10-erase-fix.sh" && regular "$CONFIG/system.sh" || fail missing-hooks
    [ "$(busybox devmem 0x1f06d4 32)" = "0x3C138044" ] || fail instructions
[ "$(busybox devmem 0x1f06e0 32)" = "0x8E64B190" ] || fail instructions
[ "$(busybox devmem 0x1f0764 32)" = "0x8C420010" ] || fail instructions
[ "$(busybox devmem 0x1efca4 32)" = "0x8E220010" ] || fail instructions
[ "$(busybox awk '$3=="recovery_norflash_erase" {print $1}' /proc/kallsyms)" = "801f06cc" ] || fail symbols
[ "$(busybox awk '$3=="jz_spi_norflash_erase_sector" {print $1}' /proc/kallsyms)" = "801efc74" ] || fail symbols
[ "$(busybox awk '$3=="direct_erase_norflash" {print $1}' /proc/kallsyms)" = "801f080c" ] || fail symbols
    pointer=$(busybox devmem 0x0043b190 32) || fail pointer
    case "$pointer" in 0x????????) ;; *) fail pointer-format;; esac
    p=$((pointer))
    [ "$p" -ge $((0x80450000)) ] && [ "$p" -le $((0x83fffd9c)) ] && [ "$((p % 4))" -eq 0 ] || fail pointer-range
    field=$((p - 0x80000000 + 16))
    [ "$(busybox devmem "$field" 32)" = 0x00001000 ] || fail fix-inactive
    [ "$(busybox devmem 0x0043b190 32)" = "$pointer" ] || fail pointer-changed
    STATUS=LIVE
}
main() {
    # The upload path is a fresh random name under the stock /tmp. Confirm
    # tmpfs before any further staging or status writes.
    [ ! -L /tmp ] || exit 1
    busybox awk '$2=="/tmp" && $3=="tmpfs" {n++} END {exit(n!=1)}' /proc/mounts || exit 1
    regular "$SELF" || exit 1
    umask 077
    mkdir "$STAGE" || exit 1
    prepare_status || exit 1
    trap finish 0
    STATUS=BUSY
    publish_status || exit 1
    STATUS=FAIL
    preflight
    cat > "$STAGE/fix" <<'CC2_PAYLOAD_END' || fail payload
#!/bin/sh
# cc2flash erase hook v1; all diagnostics stay in RAM.
fail() { echo "cc2flash: erase fix refused: $*"; exit 1; }
word() { busybox devmem "$1" 32; }
mounted() { busybox awk '$2=="/etc/conf.d" {n++; if($1!="/dev/mtdblock5" || $3!="jffs2" || $4 !~ /(^|,)rw(,|$)/)bad=1} END {exit(n!=1 || bad)}' /proc/mounts; }
[ -n "$CC2_STAGE" ] && [ "$PWD" = / ] || fail runner
busybox pidof ucamera >/dev/null && fail ucamera-running
mounted || fail config-mount
# No other mounts of this filesystem or mounts beneath config.
[ "$(busybox awk '$1=="/dev/mtdblock5" || $2 ~ /^\/etc\/conf.d\// {n++} END {print n+0}' /proc/mounts)" = 1 ] || fail mount-topology
[ "$(busybox sed '1d;y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' /proc/mtd)" = 'mtd0: 00040000 00004000 "boot"
mtd1: 00150000 00004000 "kernel"
mtd2: 00158000 00004000 "root"
mtd3: 004e8000 00004000 "system"
mtd4: 00010000 00004000 "hwconfig"
mtd5: 00020000 00004000 "config"' ] || fail partition-map
[ "$(busybox awk '$3=="recovery_norflash_erase" {print $1}' /proc/kallsyms)" = "801f06cc" ] || fail symbols
[ "$(busybox awk '$3=="jz_spi_norflash_erase_sector" {print $1}' /proc/kallsyms)" = "801efc74" ] || fail symbols
[ "$(busybox awk '$3=="direct_erase_norflash" {print $1}' /proc/kallsyms)" = "801f080c" ] || fail symbols
[ "$(word 0x1f06d4)" = "0x3C138044" ] || fail instructions
[ "$(word 0x1f06e0)" = "0x8E64B190" ] || fail instructions
[ "$(word 0x1f0764)" = "0x8C420010" ] || fail instructions
[ "$(word 0x1efca4)" = "0x8E220010" ] || fail instructions
# Stock BusyBox MD5 is a build fingerprint, not cryptographic authentication.
hash=$(busybox md5sum /dev/mtd1) || fail kernel-read
[ "$hash" = '388e256470b2ad70f4a29cc37e0fee32  /dev/mtd1' ] || fail kernel-hash
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
[ "$(word 0x1f06d4)" = "0x3C138044" ] || fail instructions
[ "$(word 0x1f06e0)" = "0x8E64B190" ] || fail instructions
[ "$(word 0x1f0764)" = "0x8C420010" ] || fail instructions
[ "$(word 0x1efca4)" = "0x8E220010" ] || fail instructions
if [ "$old" != 0x00001000 ]; then
    busybox devmem "$field" 32 0x00001000 || fail ram-write
fi
[ "$(word 0x0043b190)" = "$pointer" ] && [ "$(word "$field")" = 0x00001000 ] || fail readback
busybox mount -t jffs2 /dev/mtdblock5 /etc/conf.d || fail remount
trap - 0
mounted || fail mount-readback
[ "$(busybox sed '1d;y/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/' /proc/mtd)" = 'mtd0: 00040000 00004000 "boot"
mtd1: 00150000 00004000 "kernel"
mtd2: 00158000 00004000 "root"
mtd3: 004e8000 00004000 "system"
mtd4: 00010000 00004000 "hwconfig"
mtd5: 00020000 00004000 "config"' ] || fail geometry-changed
[ -s /etc/conf.d/serial.cfg ] || fail serial-missing
echo 'cc2flash: erase fix active; master=0x1000; partition geometry=0x4000'
CC2_PAYLOAD_END
    cat > "$STAGE/runner" <<'CC2_PAYLOAD_END' || fail payload
#!/bin/sh
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
CC2_PAYLOAD_END
    [ "$(busybox md5sum "$STAGE/fix")" = 'c6d5dd18094e9e65fc42e9afa2f40c24  '"$STAGE/fix" ] || fail staged-fix
    [ "$(busybox md5sum "$STAGE/runner")" = '057670efd1488e00ec5f3ecfd6343748  '"$STAGE/runner" ] || fail staged-runner
    case "$MODE" in
        install) install_files;;
        verify) verify_live;;
        *) fail mode;;
    esac
}
# All mutation is in functions. A truncated upload lacking this tail cannot
# execute the installer. HID CRC protects each transmitted chunk.
main

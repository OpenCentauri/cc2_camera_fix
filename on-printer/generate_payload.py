#!/usr/bin/env python3
"""Build the native tool's embedded installer from canonical hook bytes.

Python is required only on the development/build machine, never on the printer.
"""
import hashlib
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cc2camera.startup_payloads import RUNNER, erase_hook, KERNEL_MD5
from cc2camera.adb_backup import EXPECTED_PARTITIONS
from cc2camera.restore_prepare import INSTRUCTIONS, SYMBOLS

ROOT = Path(__file__).resolve().parent

def generate():
    fix = erase_hook()
    files = {'fix': fix, 'runner': RUNNER}
    payloads = '\n'.join(
        '    cat > "$STAGE/' + name + '" <<\'CC2_PAYLOAD_END\' || fail payload\n'
        + data.decode('ascii') + 'CC2_PAYLOAD_END'
        for name, data in files.items()
    )
    checks = '\n'.join(
        f'[ "$(busybox devmem {a:#x} 32)" = "0x{v:08X}" ] || fail instructions'
        for a, v in INSTRUCTIONS.items()
    ) + '\n' + '\n'.join(
        f'[ "$(busybox awk \'$3=="{n}" {{print $1}}\' /proc/kallsyms)" = "{a:08x}" ] || fail symbols'
        for n, a in SYMBOLS.items()
    )
    replacements = {
        '@KERNEL_MD5@': KERNEL_MD5,
        '@HID_MD5@': '8091751fdd4d0d50ea31901663797a86',
        '@MTD@': '\n'.join(f'mtd{i}: {size:08x} 00004000 "{name}"' for i, (name,size) in enumerate(EXPECTED_PARTITIONS)),
        '@PAYLOADS@': payloads,
        '@FIX_MD5@': hashlib.md5(fix).hexdigest(),
        '@RUNNER_MD5@': hashlib.md5(RUNNER).hexdigest(),
        '@LIVE_CHECKS@': checks,
    }
    script = (ROOT/'installer.sh.in').read_text()
    for key,value in replacements.items(): script=script.replace(key,value)
    return script

if __name__ == '__main__':
    path = ROOT/'payload/installer.sh'
    data = generate()
    if sys.argv[1:] == ['--check']:
        if path.read_text() != data: raise SystemExit('Embedded installer is stale; run on-printer/generate_payload.py')
    elif not sys.argv[1:]:
        path.write_text(data)
    else: raise SystemExit('Usage: generate_payload.py [--check]')

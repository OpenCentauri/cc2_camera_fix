"""Install bounded config hooks through online ADB, with preserved backups."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import struct
import tempfile
import zlib

from .adb_backup import AdbClient, acquire_stable, load_preserved_backup, validate_replacement_against_backup
from .protocol import ProtocolError
from .restore_prepare import validate_preparation_image, _single_camera
from .startup_payloads import RUNNER, ADB_HOOK, erase_hook

CONFIG = '/etc/conf.d'
ERASE = 0x4000
# Retain five clean logical blocks for JFFS2's allocation/GC reserves and the
# next boot's config writes. Never count dirty bytes or arbitrary FF holes.
RESERVE_BLOCKS = 5
MAX_HOOK_BYTES = 16384


@dataclass(frozen=True)
class SpacePlan:
    clean_blocks: int
    write_budget: int
    filesystem_available: int


def space_plan(config: bytes, payloads: dict[str, bytes], available: int) -> SpacePlan:
    """Require complete clean-marker blocks plus conservative write headroom.

    This is an admission check, not a power-loss or concurrent-writer guarantee.
    Completely FF blocks without clean markers are excluded: JFFS2 can queue
    them for erase before use, which is precisely the broken operation.
    """
    if len(config) != 0x20000 or not payloads or available < 0:
        raise ProtocolError('invalid config space evidence')
    header = struct.pack('<HHI', 0x1985, 0x2003, 12)
    marker = header + struct.pack('<I', (zlib.crc32(header, 0xffffffff) ^ 0xffffffff) & 0xffffffff)
    clean = sum(config[o:o+12] == marker and config[o+12:o+ERASE] == b'\xff'*(ERASE-12)
                for o in range(0, len(config), ERASE))
    # Bound uncompressed inode pages, headers, alignment, temporary + final
    # dirents, chmod, mkdir and block-end slack. Renames do not duplicate data.
    budget = 4096 + sum(len(data) + ((len(data)+4095)//4096)*128 + 1024
                        for data in payloads.values())
    budget = ((budget+ERASE-1)//ERASE)*ERASE
    if clean*ERASE < budget + RESERVE_BLOCKS*ERASE or available < budget + ERASE:
        raise ProtocolError(
            f'insufficient safe config space: {clean} clean 16-KiB blocks, '
            f'{available} filesystem bytes available; need {budget} write bytes '
            f'plus {RESERVE_BLOCKS} clean reserve blocks. No persistent write performed'
        )
    return SpacePlan(clean, budget, available)


def _checked(adb: AdbClient, script: str, *, timeout=30) -> bytes:
    """Legacy adbd does not reliably return the remote shell exit status."""
    output = adb.shell(script + '\n', timeout=timeout).replace(b'\r', b'').strip()
    if not output.endswith(b'CC2_OK'):
        raise ProtocolError('camera preflight/write did not confirm success: ' + output[-500:].decode('ascii', 'replace'))
    return output[:-6].strip()


def _available(adb: AdbClient) -> int:
    output = _checked(adb, "busybox stat -f -c '%a %S' /etc/conf.d && echo CC2_OK")
    if not re.fullmatch(rb'[0-9]+ [0-9]+', output):
        raise ProtocolError('invalid filesystem free-space report')
    blocks, size = map(int, output.split())
    if size != 4096 or blocks > 32:
        raise ProtocolError('unexpected config statfs geometry')
    return blocks*size


def _read_optional(adb: AdbClient, remote: str, local: Path) -> bytes | None:
    quoted = shlex.quote(remote)
    kind = _checked(adb, f'if [ -L {quoted} ]; then echo link; elif [ -f {quoted} ]; then echo file; elif [ -e {quoted} ]; then echo other; else echo absent; fi; echo CC2_OK')
    if kind == b'absent':
        return None
    if kind != b'file':
        raise ProtocolError(f'refusing non-regular managed path: {remote}')
    size = _checked(adb, f'busybox stat -c %s {quoted} && echo CC2_OK')
    if not size.isdigit() or int(size) > MAX_HOOK_BYTES:
        raise ProtocolError(f'oversized managed hook: {remote}')
    mode = _checked(adb, f'busybox stat -c %a {quoted} && echo CC2_OK')
    if mode != b'755':
        raise ProtocolError(f'unexpected hook permissions: {remote}; expected 755')
    adb.pull(remote, local)
    data = local.read_bytes()
    if len(data) != int(size):
        raise ProtocolError('hook changed during preflight')
    return data


def install_startup(adb: AdbClient, backup: Path, *, functionality: str, progress=None) -> SpacePlan | None:
    """Install one feature and the shared runner; never remount or patch live RAM.

    Requires explicit caller consent and already-online root ADB. Installation
    performs ordinary JFFS2 writes, not raw flash erasure or firmware flashing.
    Unknown system.sh content is refused; there is no legacy migration path.
    """
    if functionality not in ('adb', 'erase-fix'):
        raise ProtocolError('unknown startup functionality')
    preserved, _ = load_preserved_backup(backup)
    validate_preparation_image(preserved)  # local validation before transport
    adb.ensure_available()
    _single_camera(adb)
    live, _ = acquire_stable(adb, progress=progress)
    validate_preparation_image(live)
    validate_replacement_against_backup(live, preserved)
    _, parts = adb.identity_and_partitions()
    if any(p.erase_size != ERASE for p in parts):
        raise ProtocolError('unexpected partition erase geometry')
    _checked(adb, """[ "$(id -u)" = 0 ] &&
busybox awk '$2=="/etc/conf.d" && $1=="/dev/mtdblock5" && $3=="jffs2" && $4 ~ /(^|,)rw(,|$)/ {n++} END {exit(n!=1)}' /proc/mounts &&
busybox awk '$2=="/tmp" && $3=="tmpfs" {n++} END {exit(n!=1)}' /proc/mounts &&
[ ! -L /etc/conf.d ] && [ ! -L /etc/conf.d/enabled ] &&
{ [ ! -e /etc/conf.d/enabled ] || [ -d /etc/conf.d/enabled ]; } && echo CC2_OK""")
    name, data = ('90-adb.sh', ADB_HOOK) if functionality == 'adb' else ('10-erase-fix.sh', erase_hook())
    requested = {'enabled/'+name: data, 'system.sh': RUNNER}
    with tempfile.TemporaryDirectory(prefix='cc2-hooks-') as directory:
        root = Path(directory)
        payloads = {}
        for i, (relative, content) in enumerate(requested.items()):
            current = _read_optional(adb, f'{CONFIG}/{relative}', root/f'old-{i}')
            if current is not None and current != content:
                raise ProtocolError(f'unknown existing {CONFIG}/{relative}; preserve and resolve it explicitly; no migration or overwrite')
            if current is None:
                payloads[relative] = content
        if not payloads:
            return None
        plan = space_plan(live[0x7e0000:], payloads, _available(adb))
        # All staging occurs on verified tmpfs. Never push straight into config.
        stage_bytes = _checked(adb, 'umask 077; busybox mktemp -d /tmp/cc2-install.XXXXXX && echo CC2_OK')
        if not re.fullmatch(rb'/tmp/cc2-install\.[A-Za-z0-9]{6}', stage_bytes):
            raise ProtocolError('invalid RAM staging directory')
        stage = stage_bytes.decode('ascii')
        try:
            for i, (relative, content) in enumerate(payloads.items()):
                local = root/f'new-{i}'
                local.write_bytes(content)
                adb.run('push', str(local), f'{stage}/{i}')
                adb.pull(f'{stage}/{i}', root/'readback')
                if (root/'readback').read_bytes() != content:
                    raise ProtocolError('RAM staging readback mismatch; config untouched')
            # Check for concurrent config writers after staging, before first
            # persistent write. We do not freeze or remount a running camera.
            _single_camera(adb)
            _checked(adb, 'sync && echo CC2_OK')
            adb.pull('/dev/mtd5', root/'config-now')
            now = (root/'config-now').read_bytes()
            if now != live[0x7e0000:]:
                raise ProtocolError('config changed during installation preflight; config untouched')
            plan = space_plan(now, payloads, _available(adb))
            # Require the same absence/content immediately before writing.
            guards = [f'[ ! -L {CONFIG}/enabled ]',
                      f'{{ [ ! -e {CONFIG}/enabled ] || [ -d {CONFIG}/enabled ]; }}']
            for i, relative in enumerate(requested):
                dest = f'{CONFIG}/{relative}'
                if relative in payloads:
                    guards += [f'[ ! -e {dest} ]', f'[ ! -L {dest} ]']
                else:
                    # Content already checked host-side; stage exact copy for cmp.
                    local = root/f'existing-{i}'
                    local.write_bytes(requested[relative])
                    adb.run('push', str(local), f'{stage}/existing-{i}')
                    guards += [f'[ ! -L {dest} ]', f'busybox cmp -s {dest} {stage}/existing-{i}']
            guards += [f'[ ! -e {CONFIG}/.cc2-new-{i} ] && [ ! -L {CONFIG}/.cc2-new-{i} ]' for i in range(len(payloads))]
            guards += [f'[ "$(busybox stat -f -c %a {CONFIG})" -ge {(plan.write_budget+ERASE)//4096} ]']
            _checked(adb, ' &&\n'.join(guards) + ' && echo CC2_OK')
            # Feature first, runner last: an interrupted first installation never
            # exposes a runner referring to an incomplete hook.
            script = 'set -e\numask 022\n' + ' &&\n'.join(guards) + ' || exit 1\n'
            script += f'mkdir -p {CONFIG}/enabled\n'
            for i, relative in enumerate(payloads):
                dest = f'{CONFIG}/{relative}'
                # Fixed temp names are refused rather than recycled after failure.
                temp = f'{CONFIG}/.cc2-new-{i}'
                script += (f'[ ! -e {temp} ] && [ ! -L {temp} ] || exit 1\n'
                           f'cp {stage}/{i} {temp}\nchmod 755 {temp}\n'
                           f'busybox cmp -s {stage}/{i} {temp}\nsync\n'
                           f'mv {temp} {dest}\nsync\n')
            script += 'echo CC2_OK'
            _checked(adb, script, timeout=120)
            for i, (relative, content) in enumerate(requested.items()):
                _checked(adb, f'[ ! -L {CONFIG}/{relative} ] && [ "$(busybox stat -c %a {CONFIG}/{relative})" = 755 ] && echo CC2_OK')
                adb.pull(f'{CONFIG}/{relative}', root/f'final-{i}')
                if (root/f'final-{i}').read_bytes() != content:
                    raise ProtocolError('persistent hook readback mismatch; stop, preserve diagnostics; do not retry blindly')
            return plan
        finally:
            # Only tmpfs cleanup. Never unlink partially installed config data.
            adb.shell(f'rm -rf {stage}')

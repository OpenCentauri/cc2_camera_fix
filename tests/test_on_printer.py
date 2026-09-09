"""Offline native-tool payload contracts; no USB devices or firmware fixtures."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hid_payload_generator', ROOT/'on-printer/generate_payload.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class PayloadGenerationTests(unittest.TestCase):
    def test_generated_payload_matches_canonical_hooks(self):
        data = generator.generate()
        self.assertEqual((ROOT/'on-printer/payload/installer.sh').read_text(), data)
        self.assertIn(generator.RUNNER.decode(), data)
        self.assertIn(generator.erase_hook().decode(), data)
        self.assertLess(len(data), 32768)
        self.assertNotIn('@PAYLOADS@', data)


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('sh') and shutil.which('md5sum'), 'Linux shell tools required')
class CameraShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cc2-hid-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'config'
        self.stage = self.root/'stage'
        self.config.mkdir()
        self.stage.mkdir()
        (self.config/'serial.cfg').write_text('synthetic identity\n')
        (self.stage/'fix').write_bytes(generator.erase_hook())
        (self.stage/'runner').write_bytes(generator.RUNNER)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        (self.bin/'busybox').write_text('#!/bin/sh\nexec "$@"\n')
        (self.bin/'id').write_text('#!/bin/sh\necho 0\n')
        (self.bin/'sync').write_text('#!/bin/sh\nexit 0\n')
        for p in self.bin.iterdir(): p.chmod(0o755)
        script = generator.generate().rsplit('\nmain\n', 1)[0] + '\n'
        script = script.replace('PATH=/bin:/sbin:/usr/bin:/usr/sbin', f'PATH={self.bin}:/bin:/usr/bin')
        script = script.replace('@TOKEN@', '0123456789abcdef').replace('@MODE@', 'install')
        mounts = self.root/'mounts'
        mounts.write_text(f'/dev/mtdblock5 {self.config} jffs2 rw 0 0\n')
        mtd = self.root/'mtd'
        mtd.write_text('dev: size erasesize name\n'+'\n'.join(
            f'mtd{i}: {size:08x} 00004000 "{name}"' for i,(name,size) in enumerate(generator.EXPECTED_PARTITIONS))+'\n')
        for path, content in (('/dev/mtd1', b'synthetic kernel'), ('/bin/hid_update', b'synthetic hid'), ('/dev/mtd5', b'stable synthetic config')):
            local = self.root/path.rsplit('/',1)[-1]
            local.write_bytes(content)
            script = script.replace(path, str(local))
            if path == '/dev/mtd1': script=script.replace(generator.KERNEL_MD5,hashlib.md5(content).hexdigest())
            if path == '/bin/hid_update': script=script.replace('8091751fdd4d0d50ea31901663797a86',hashlib.md5(content).hexdigest())
        script = script.replace('/etc/conf.d',str(self.config)).replace('/proc/mounts',str(mounts)).replace('/proc/mtd',str(mtd))
        self.script = script + f'\nSTAGE={self.stage}\n'

    def run_shell(self, command='preflight; install_files', modifications=''):
        result = subprocess.run(['sh'], input=self.script+modifications+'\n'+command+'\nprintf "STATUS=%s\\n" "$STATUS"\n', text=True, capture_output=True, timeout=10)
        return result


    def status_setup(self):
        version = self.root/'version.txt'
        self.script = self.script.replace('/tmp/version.txt', str(version))
        (self.bin/'sleep').write_text('#!/bin/sh\nexit 0\n')
        (self.bin/'sleep').chmod(0o755)
        return version

    def test_status_restores_existing_file_or_removes_created_file(self):
        version = self.status_setup()
        for existing in (False, True):
            self.stage.mkdir(exist_ok=True)
            if existing:
                version.write_bytes(b'original version\nsecond line\x00')
                version.chmod(0o640)
            result = self.run_shell('prepare_status || exit 1; STATUS=BUSY; publish_status || exit 1; cat '+str(version)+'; STATUS=DONE; finish || exit 1')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('0123456789abcdef:BUSY', result.stdout)
            if existing:
                self.assertEqual(version.read_bytes(), b'original version\nsecond line\x00')
                self.assertEqual(version.stat().st_mode & 0o777, 0o640)
            else:
                self.assertFalse(version.exists())
            self.assertFalse(self.stage.exists())
            self.assertFalse((self.config/'enabled').exists())

    def test_status_refuses_unsafe_paths_without_changing_targets(self):
        version = self.status_setup()
        target = self.root/'target'
        target.write_text('untouched')
        for kind in ('link', 'dangling', 'directory', 'fifo', 'oversized'):
            if kind == 'link': version.symlink_to(target)
            elif kind == 'dangling': version.symlink_to(self.root/'absent')
            elif kind == 'directory': version.mkdir()
            elif kind == 'fifo': os.mkfifo(version)
            else: version.write_bytes(b'x'*4097)
            result = self.run_shell('prepare_status || exit 1')
            self.assertNotEqual(result.returncode, 0, kind)
            self.assertEqual(target.read_text(), 'untouched')
            self.assertFalse((self.stage/'version').exists())
            self.assertFalse((self.config/'enabled').exists())
            if kind == 'directory': version.rmdir()
            else: version.unlink()

    def test_status_cleanup_preserves_unexpected_replacement(self):
        version = self.status_setup()
        version.write_text('original')
        (self.bin/'sleep').write_text('#!/bin/sh\nprintf other > '+str(version)+'\n')
        result = self.run_shell('prepare_status || exit 1; STATUS=DONE; finish || exit 1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(version.read_text(), 'other')
        self.assertEqual((self.stage/'version').read_text(), 'original')
        self.assertFalse((self.config/'enabled').exists())

    def test_success_then_idempotency_without_persistent_changes(self):
        result = self.run_shell()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('STATUS=DONE', result.stdout)
        paths = [self.config/'system.sh',self.config/'enabled/10-erase-fix.sh']
        self.assertEqual(paths[0].read_bytes(),generator.RUNNER)
        self.assertEqual(paths[1].read_bytes(),generator.erase_hook())
        before = [(p.stat().st_mtime_ns,p.read_bytes()) for p in paths]
        result = self.run_shell()
        self.assertIn('STATUS=SAME',result.stdout)
        self.assertEqual(before,[(p.stat().st_mtime_ns,p.read_bytes()) for p in paths])

    def test_unknown_hook_or_symlink_never_writes(self):
        dest = self.config/'system.sh'
        for link in (False, True):
            if link: dest.symlink_to(self.stage/'runner')
            else: dest.write_text('unknown script'); dest.chmod(0o755)
            result = self.run_shell()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertFalse((self.config/'enabled').exists())
            dest.unlink()

    def test_kernel_fingerprint_and_partition_map_refuse_before_writes(self):
        for name in ('mtd1','mtd'):
            p=self.root/name
            original=p.read_bytes()
            p.write_bytes(b'unsupported')
            result=self.run_shell()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertFalse((self.config/'enabled').exists())
            p.write_bytes(original)

    def test_partial_copy_never_installs_boot_entry_point(self):
        # Simulated ENOSPC after a partial persistent file creation.
        (self.bin/'cp').write_text('#!/bin/sh\nprintf truncated > "$2"\nexit 1\n')
        (self.bin/'cp').chmod(0o755)
        result=self.run_shell(modifications="trap 'echo STATUS=$STATUS' 0")
        self.assertNotEqual(result.returncode,0)
        self.assertIn('STATUS=PART',result.stdout)
        self.assertFalse((self.config/'system.sh').exists())
        self.assertFalse((self.config/'enabled/10-erase-fix.sh').exists())
        self.assertTrue((self.config/'.cc2-hid-fix').exists())

    def test_readback_mismatch_never_renames(self):
        (self.bin/'cp').write_text('#!/bin/sh\nprintf corrupted > "$2"\n')
        (self.bin/'cp').chmod(0o755)
        result=self.run_shell()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('temporary-readback',result.stdout)
        self.assertFalse((self.config/'system.sh').exists())
        self.assertFalse((self.config/'enabled/10-erase-fix.sh').exists())

    def test_stale_partial_install_refused_without_cleanup(self):
        leftover=self.config/'.cc2-hid-fix'
        leftover.write_text('preserve diagnostics')
        result=self.run_shell()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(leftover.read_text(),'preserve diagnostics')
        self.assertFalse((self.config/'enabled').exists())

    def test_changed_config_stops_before_first_write(self):
        script='''
count=0
preflight() { count=$((count+1)); if [ "$count" = 2 ]; then echo changed > "@CONFIG@"; fi; }
'''.replace('@CONFIG@',str(self.root/'mtd5'))
        result=self.run_shell(modifications=script)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('config-changing',result.stdout)
        self.assertFalse((self.config/'enabled').exists())

    def test_verify_reads_active_correction_without_writing_config(self):
        self.assertEqual(self.run_shell().returncode, 0)
        symbols = self.root/'kallsyms'
        symbols.write_text(''.join(f'{a:08x} T {n}\n' for n,a in generator.SYMBOLS.items()))
        self.script = self.script.replace('/proc/kallsyms', str(symbols))
        field = self.root/'field'
        field.write_text('0x00001000\n')
        cases = '\n'.join(f'{a:#x}) echo 0x{v:08X};;' for a,v in generator.INSTRUCTIONS.items())
        wrapper = '#!/bin/sh\nif [ "$1" = devmem ]; then\n[ "$#" = 3 ] || exit 89\ncase "$2" in\n' + cases
        wrapper += f'\n0x0043b190) echo 0x80450000;;\n{0x450010}) cat {field};;\n*) exit 1;;\nesac\nelse exec "$@"; fi\n'
        (self.bin/'busybox').write_text(wrapper)
        before = {str(p):p.read_bytes() for p in self.config.rglob('*') if p.is_file()}
        result = self.run_shell('preflight; verify_live')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('STATUS=LIVE',result.stdout)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.config.rglob('*') if p.is_file()})
        field.write_text('0x00004000\n')
        result = self.run_shell('preflight; verify_live')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('fix-inactive',result.stdout)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.config.rglob('*') if p.is_file()})

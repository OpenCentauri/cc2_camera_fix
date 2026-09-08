"""Offline native-tool payload contracts; no USB devices or firmware fixtures."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
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


@unittest.skipUnless(os.name == 'posix' and shutil.which('sh') and shutil.which('md5sum'), 'POSIX shell tools required')
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

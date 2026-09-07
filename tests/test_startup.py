"""Offline safety contracts for config hook installation and boot payloads."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from cc2flash import startup, cli
from cc2flash import startup_payloads as payloads
from cc2flash.image import jffs2_crc
from cc2flash.protocol import ProtocolError


def clean_config(count=7):
    h = struct.pack('<HHI', 0x1985, 0x2003, 12)
    clean = h + struct.pack('<I', jffs2_crc(h)) + b'\xff'*(16384-12)
    return b'\x00'*((8-count)*16384) + clean*count


class SpaceTests(unittest.TestCase):
    def test_each_feature_and_both_fit_with_six_clean_blocks(self):
        for files in ({'system.sh':payloads.RUNNER, 'adb':payloads.ADB_HOOK},
                      {'system.sh':payloads.RUNNER, 'fix':payloads.erase_hook()},
                      {'fix':payloads.erase_hook()}):
            p = startup.space_plan(clean_config(6), files, 32768)
            self.assertEqual(p.write_budget, 16384)

    def test_exhaustion_and_reclaimable_or_unmarked_space_refused(self):
        for config, available in ((clean_config(5),131072), (b'\xff'*131072,131072),
                                  (clean_config(),32767), (clean_config()[:-1],131072)):
            with self.subTest(available=available), self.assertRaises(ProtocolError):
                startup.space_plan(config, {'runner':payloads.RUNNER}, available)

    def test_marker_and_whole_block_must_be_valid(self):
        config = bytearray(clean_config(6))
        config[2*16384+8] ^= 1
        with self.assertRaises(ProtocolError):
            startup.space_plan(bytes(config), {'runner':payloads.RUNNER},131072)
        config = bytearray(clean_config(6)); config[3*16384-1] = 0
        with self.assertRaises(ProtocolError):
            startup.space_plan(bytes(config), {'runner':payloads.RUNNER},131072)


class InstallerTests(unittest.TestCase):
    def setup_install(self, stack, *, existing=False):
        adb = mock.Mock()
        live = b'\x00'*0x7e0000 + clean_config()
        stack.enter_context(mock.patch.object(startup, 'load_preserved_backup', return_value=(live,{})))
        stack.enter_context(mock.patch.object(startup, 'validate_preparation_image'))
        stack.enter_context(mock.patch.object(startup, 'validate_replacement_against_backup'))
        stack.enter_context(mock.patch.object(startup, '_single_camera'))
        stack.enter_context(mock.patch.object(startup, 'acquire_stable', return_value=(live,{})))
        adb.identity_and_partitions.return_value = ('root',[mock.Mock(erase_size=0x4000)])
        stack.enter_context(mock.patch.object(startup, '_read_optional', side_effect=lambda a,p,l: (payloads.RUNNER if p.endswith('system.sh') else payloads.ADB_HOOK) if existing else None))
        stack.enter_context(mock.patch.object(startup, '_available', return_value=65536))
        files = {}
        events = []
        def run(*args):
            events.append(('run',args))
            if args[0]=='push': files[args[2]]=Path(args[1]).read_bytes()
        adb.run.side_effect = run
        def pull(remote, local):
            events.append(('pull',remote))
            if remote=='/dev/mtd5': data=live[0x7e0000:]
            elif remote==startup.CONFIG+'/system.sh': data=payloads.RUNNER
            elif remote==startup.CONFIG+'/enabled/90-adb.sh': data=payloads.ADB_HOOK
            else: data=files[remote]
            local.write_bytes(data)
        adb.pull.side_effect = pull
        def checked(a,script,**kw):
            events.append(('checked',script))
            return b'/tmp/cc2-install.ABC123' if 'mktemp' in script else b''
        check = stack.enter_context(mock.patch.object(startup,'_checked',side_effect=checked))
        return adb,events,check

    def test_success_stages_checks_then_installs_runner_last_and_reads_back(self):
        with ExitStack() as stack:
            adb,events,_ = self.setup_install(stack)
            result=startup.install_startup(adb,Path('backup.zip'),functionality='adb')
        self.assertEqual(result.clean_blocks,7)
        write=next((i,s) for i,(k,s) in enumerate(events) if k=='checked' and s.startswith('set -e'))
        self.assertLess(write[1].index('mv /etc/conf.d/.cc2-new-0'),write[1].index('mv /etc/conf.d/.cc2-new-1'))
        self.assertIn(('pull','/dev/mtd5'),events[:write[0]])
        self.assertIn(('pull','/etc/conf.d/system.sh'),events[write[0]+1:])
        self.assertNotIn('devmem',write[1]);self.assertNotIn('umount',write[1])

    def test_low_space_never_pushes_or_persistently_writes(self):
        with ExitStack() as stack:
            adb,events,_ = self.setup_install(stack)
            stack.enter_context(mock.patch.object(startup,'_available',return_value=0))
            with self.assertRaises(ProtocolError): startup.install_startup(adb,Path('backup.zip'),functionality='adb')
            adb.run.assert_not_called()
        self.assertFalse(any(k=='checked' and s.startswith('set -e') for k,s in events))

    def test_unknown_hook_and_unsupported_kernel_refuse_before_writes(self):
        for gate in ('hook','kernel'):
            with self.subTest(gate=gate), ExitStack() as stack:
                adb,events,_=self.setup_install(stack)
                if gate=='hook': stack.enter_context(mock.patch.object(startup,'_read_optional',return_value=b'custom'))
                else: stack.enter_context(mock.patch.object(startup,'validate_preparation_image',side_effect=ProtocolError('kernel')))
                with self.assertRaises(ProtocolError): startup.install_startup(adb,Path('backup.zip'),functionality='adb')
                adb.run.assert_not_called()
                if gate=='kernel': adb.ensure_available.assert_not_called()

    def test_exact_files_are_idempotent_without_staging_or_space_writes(self):
        with ExitStack() as stack:
            adb,_,_=self.setup_install(stack,existing=True)
            self.assertIsNone(startup.install_startup(adb,Path('backup.zip'),functionality='adb'))
            adb.run.assert_not_called()

    def test_changed_config_or_final_space_refuses_before_persistent_write(self):
        for reason in ('config','space','staging','readback'):
            with self.subTest(reason=reason),ExitStack() as stack:
                adb,events,_=self.setup_install(stack)
                original=adb.pull.side_effect
                def pull(remote,local):
                    original(remote,local)
                    if ((reason=='config' and remote=='/dev/mtd5') or
                        (reason=='staging' and remote.startswith('/tmp/')) or
                        (reason=='readback' and remote.endswith('/system.sh'))): local.write_bytes(b'changed')
                adb.pull.side_effect=pull
                if reason=='space': stack.enter_context(mock.patch.object(startup,'_available',side_effect=[65536,0]))
                with self.assertRaises(ProtocolError):startup.install_startup(adb,Path('backup.zip'),functionality='adb')
                written=any(k=='checked' and s.startswith('set -e') for k,s in events)
                self.assertEqual(written,reason=='readback')

    def test_offline_adb_and_bad_partition_geometry_never_stage(self):
        from cc2flash.adb_backup import AdbUnavailable
        for reason in ('offline', 'geometry'):
            with self.subTest(reason=reason), ExitStack() as stack:
                adb, events, _ = self.setup_install(stack)
                if reason == 'offline':
                    adb.ensure_available.side_effect = AdbUnavailable('offline')
                else:
                    adb.identity_and_partitions.return_value = ('root', [mock.Mock(erase_size=4096)])
                with self.assertRaises(ProtocolError):
                    startup.install_startup(adb, Path('backup.zip'), functionality='adb')
                adb.run.assert_not_called()

    def test_final_shell_guard_failure_does_not_create_enabled(self):
        with ExitStack() as stack:
            adb, events, _ = self.setup_install(stack)
            startup.install_startup(adb, Path('backup.zip'), functionality='adb')
        script = next(s for k,s in events if k == 'checked' and s.startswith('set -e'))
        with tempfile.TemporaryDirectory() as directory:
            # Actual POSIX shell execution: force the first guard to fail.
            config = Path(directory)/'config'
            config.mkdir()
            (config/'enabled').symlink_to(Path(directory)/'absent')
            result = subprocess.run(['sh', '-c', script.replace('/etc/conf.d', 'config')],
                                    cwd=directory, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual([p.name for p in config.iterdir()], ['enabled'])
            self.assertNotIn(b'CC2_OK', result.stdout)

    def test_cli_backup_and_consent_before_installer(self):
        for command in ('install-adb-startup','install-erase-fix'):
            with mock.patch.object(cli,'install_startup') as install,redirect_stderr(io.StringIO()),redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):cli.main([command,'--yes'])
                install.assert_not_called()
                with mock.patch.object(cli.sys.stdin,'isatty',return_value=False):
                    self.assertEqual(cli.main([command,'--backup','backup.zip']),2)
                install.assert_not_called()
                self.assertEqual(cli.main([command,'--backup','backup.zip','--yes']),0)
                self.assertEqual(install.call_args.kwargs['functionality'],'adb' if command=='install-adb-startup' else 'erase-fix')


class PayloadTests(unittest.TestCase):
    def test_real_mount_guards(self):
        import re
        good = '/dev/mtdblock5 /etc/conf.d jffs2 rw,relatime 0 0\n'
        cases = [(good, True), (good.replace('rw,relatime', 'relatime,rw'), True),
                 (good.replace('rw,', 'ro,'), False),
                 (good.replace('mtdblock5', 'mtdblock4'), False),
                 (good.replace('jffs2', 'tmpfs'), False), ('', False),
                 (good * 2, False),
                 (good + good.replace('mtdblock5', 'mtdblock4'), False),
                 (good.replace('rw,', 'notrw,'), False)]
        for payload in (payloads.RUNNER, payloads.erase_hook()):
            programs = re.findall(r"busybox awk '([^']+)' /proc/mounts", payload.decode())
            guard = next(p for p in programs if 'n!=1 || bad' in p)
            for mounts, accepted in cases:
                with self.subTest(mounts=mounts, payload=payload[:40]):
                    result = subprocess.run(['awk', guard], input=mounts, text=True,
                                            capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_runner_holds_before_next_hook_on_bad_mount(self):
        for mount in ('/dev/mtdblock5 /etc/conf.d jffs2 ro 0 0',
                      '/dev/mtdblock4 /etc/conf.d jffs2 rw 0 0', ''):
            with self.subTest(mount=mount), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'enabled').mkdir()
                (root / 'enabled/10-first').write_bytes(b'exit 0\n')
                (root / 'enabled/90-next').write_bytes(b'echo SHOULD_NOT_RUN\n')
                (root / 'mounts').write_bytes((mount + '\n').encode())
                worker = payloads.RUNNER.decode().split('CC2_STAGE=$2', 1)[1]
                worker = worker.replace('/proc/mounts', '"$CC2_STAGE/mounts"')
                worker = worker.replace('/bin/adbd &', 'echo RECOVERY_ADB')
                # Bound the infinite hold; keep the real guard and dispatch logic.
                worker = worker.replace('sleep 60', 'exit 73')
                result = subprocess.run(['sh', '-c', 'busybox() { "$@"; }\n' + worker],
                    env=dict(os.environ, CC2_STAGE=root.resolve().as_posix()),
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                self.assertIn('RECOVERY_ADB', result.stdout)
                self.assertNotIn('SHOULD_NOT_RUN', result.stdout)

    def test_shell_syntax(self):
        for content in (payloads.RUNNER,payloads.ADB_HOOK,payloads.erase_hook()):
            r=subprocess.run(['sh','-n'],input=content,capture_output=True,timeout=5)
            self.assertEqual(r.returncode,0,r.stderr)

    def test_unmount_failure_and_late_start_never_write_ram(self):
        # Replace read-only environment paths and BusyBox with a shell shim.
        # Hash output is mocked; this verifies the control flow.
        for scenario in ('late','unmount-fail','ok','bad-pointer','bad-kernel','write-readback','bad-instruction','mount-fail','already-patched','bad-geometry','bad-name'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                script=payloads.erase_hook().decode()
                # Execute real guard/order logic; substitute platform observations.
                script=script.replace('mounted || fail config-mount', ':')
                script=script.replace('mounted || fail mount-readback', ':')
                script=script.replace('[ -s /etc/conf.d/serial.cfg ]', ':')
                shim='''
busybox() {
 case "$1" in
 pidof) [ "$SCENARIO" = late ]; return;;
 awk) case "$*" in
 *kallsyms*) case "$*" in *recovery_norflash_erase*) echo 801f06cc;; *erase_sector*) echo 801efc74;; *direct_erase*) echo 801f080c;; esac;;
 *'n+0'*) echo 1;; esac; return 0;;
 sed) sed "$2" "$CC2_STAGE/mtd"; return;;
 md5sum) if [ "$SCENARIO" = bad-kernel ]; then echo bad; else echo "@HASH@  /dev/mtd1"; fi; return;;
 devmem) case "$2" in
 0x1f06d4) if [ "$SCENARIO" = bad-instruction ]; then echo 0x00000000; else echo 0x3C138044; fi;; 0x1f06e0) echo 0x8E64B190;;
 0x1f0764) echo 0x8C420010;; 0x1efca4) echo 0x8E220010;;
 0x0043b190) if [ "$SCENARIO" = bad-pointer ]; then echo 0x80000000; else echo 0x81000000; fi;;
 16777232) if [ "$#" = 4 ]; then echo write >>"$CC2_STAGE/events"; touch "$CC2_STAGE/patched";
 elif { [ -f "$CC2_STAGE/patched" ] || [ "$SCENARIO" = already-patched ]; } && [ "$SCENARIO" != write-readback ]; then echo 0x00001000; else echo 0x00004000; fi;;
 *) return 1;; esac; return;;
 umount) echo unmount >>"$CC2_STAGE/events"; [ "$SCENARIO" != unmount-fail ]; return;;
 mount) echo mount >>"$CC2_STAGE/events"; [ "$SCENARIO" != mount-fail ]; return;;
 esac
 return 1
}
sync() { :; }
'''.replace('@HASH@',payloads.KERNEL_MD5)
                # Match procfs LF bytes even when the host defaults to CRLF.
                (root/'mtd').write_bytes(('dev:    size   erasesize  name\n'+'\n'.join(f'mtd{i}: {s:08x} 00004000 "{n.upper() if n == "hwconfig" else n}"' for i,(n,s) in enumerate(payloads.EXPECTED_PARTITIONS))+'\n').encode('ascii'))
                if scenario == 'bad-geometry':
                    (root/'mtd').write_bytes((root/'mtd').read_bytes().replace(b'00004000', b'00001000'))
                if scenario == 'bad-name':
                    (root/'mtd').write_bytes((root/'mtd').read_bytes().replace(b'HWCONFIG', b'OTHER'))
                # Let the shell resolve its own POSIX root (Git sh on Windows),
                # retain the host environment, and pass a shell-readable path.
                env = dict(os.environ, CC2_STAGE=root.resolve().as_posix(), SCENARIO=scenario)
                result=subprocess.run(['sh','-c','cd / || exit 1\n'+shim+script],
                                      cwd=directory, env=env, text=True, capture_output=True, timeout=10)
                events=(root/'events').read_text().splitlines() if (root/'events').exists() else []
                if scenario=='ok':
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(events,['unmount','write','mount'])
                elif scenario=='already-patched':
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(events,['unmount','mount'])
                elif scenario=='mount-fail':
                    self.assertNotEqual(result.returncode,0)
                    self.assertEqual(events,['unmount','write','mount','mount'])
                elif scenario=='write-readback':
                    self.assertNotEqual(result.returncode,0);self.assertEqual(events,['unmount','write','mount'])
                else:
                    self.assertNotEqual(result.returncode,0,result.stdout)
                    self.assertNotIn('write',events)
                    if scenario=='unmount-fail':self.assertEqual(events,['unmount'])

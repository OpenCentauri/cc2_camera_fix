# Migrating from cc2flash to cc2camera

The tool is now named `cc2camera`: it maintains the CC2's stock camera, with
inspection, backup, startup hooks, image repair, and restore workflows.

- Use `cc2camera` wherever you previously used `cc2flash`. Subcommands and
  options are unchanged; the old command and Python import name are not aliases.
- On Windows, download `cc2camera.exe` and its `cc2camera.exe.sha256` checksum
  from a release containing the rename. In PowerShell use `.\cc2camera.exe`.
- For Python, create a fresh virtual environment and follow the
  [installation guide](INSTALLATION.md) with a release containing `cc2camera`.
  The distribution, console command, and Python package are all `cc2camera`;
  module invocation is `python -m cc2camera` and imports use `cc2camera`.
  Installing into an environment containing `cc2flash` does not uninstall that
  separate distribution. To reuse it, first run `python -m pip uninstall cc2flash`
  with that environment's Python, then install `cc2camera`.
- Update scripts, shortcuts, and automation to the new command or module name.

Keep your existing backups and recovery files. The backup format identifier is
still `cc2flash-backup-v2`; do not edit manifests or repack archives. Existing
backups retain the same validation requirements. Recovery filenames and device
confirmation phrases are unchanged.

Installed v1 startup scripts remain byte-for-byte compatible. Their comments
and camera-side log messages still say `cc2flash`; this is expected, including
in `/tmp/cc2-hooks.log`. Do not replace these scripts just to rename their log
prefix: the installer deliberately recognizes only exact managed contents.
The hardware-validated temporary ADB bootstrap payload also retains its internal
`.cc2flash-adbd-bootstrap` path. These are compatibility identifiers, not CLI
entry points.

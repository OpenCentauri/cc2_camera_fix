# Install cc2flash

First [identify your camera](../README.md#first-identify-your-camera).
Installing this tool does not change the camera.

## Windows: standalone executable

1. Open the [GitHub Releases page](https://github.com/phryneas/cc2_camera_fix/releases).
2. Choose a tested release and expand **Assets**.
3. Download **cc2flash.exe** to a new folder, for example `Documents\CC2-camera`.
   The executable is for 64-bit Intel/AMD Windows and includes Python and HID
   support. You do not need Python, Git, pip, or a repository checkout.
4. In File Explorer, open that folder, click its address bar, type `cmd`,
   and press Enter. This opens Command Prompt in the correct folder.
5. Paste this command and press Enter:

```bat
cc2flash.exe --version
```

It should print a version number. Keep this window open for the recovery
instructions. Do not double-click the executable: it is a command-line tool.
In PowerShell, prefix a local executable with `.\`, for example
`.\cc2flash.exe --version`.

The executable is unsigned; Windows may show a reputation warning. Check that
you obtained it from this repository's intended release. Do not disable your
antivirus. If you are not comfortable running it, use the Python route below.
Each release build also attaches `cc2flash.exe.sha256`; you can compare its
digest with `certutil -hashfile cc2flash.exe SHA256`. This checks download
integrity, not publisher identity.

Assets appear only after the release build succeeds. If a release has no
executable, inspect its Actions result or use Python; the source-code ZIP is
not a standalone executable.

## USB workflows also need ADB

Offline image inspection/building and external-programmer recovery do not need
ADB. USB startup, backup, and restore need Google's
[SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools).

On Windows, download and extract the Windows ZIP. Copy the **whole extracted
platform-tools folder** into your CC2-camera folder; keep its DLLs beside
`adb.exe`. There is no need to edit PATH. Test:

```bat
platform-tools\adb.exe version
cc2flash.exe devices --adb "platform-tools\adb.exe"
```

Add `--adb "platform-tools\adb.exe"` to connected-camera commands in the guide.
For example:

```bat
cc2flash.exe start-adb --adb "platform-tools\adb.exe"
cc2flash.exe backup backup.zip --adb "platform-tools\adb.exe"
```

Do not add ADB options to offline commands, including `restore --dry-run`.
On Linux/macOS, download the matching Platform Tools archive and extract it;
use `--adb "/path/to/platform-tools/adb"` if it is not on PATH.
An offline ADB camera is expected before `start-adb`; an authorization,
driver, or device-ambiguity error is not equivalent to that state.

## Python alternative: install directly from GitHub

Use Python 3.10 or newer. No Git or manual repository download is required.
Choose a tested release tag from Releases. In the command below, replace
`TAG` with that tag (including its leading `v`, if present).

Windows Command Prompt, in your working folder:

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install "cc2flash @ https://github.com/phryneas/cc2_camera_fix/archive/refs/tags/TAG.zip"
.venv\Scripts\python.exe -m cc2flash --version
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install "cc2flash @ https://github.com/phryneas/cc2_camera_fix/archive/refs/tags/TAG.zip"
.venv/bin/python -m cc2flash --version
```

Use that same Python path followed by `-m cc2flash` in place of `cc2flash`
throughout the guide. No environment activation or PATH edit is needed.
HID support is installed with the package. ADB remains separate.
On Linux, your distribution may require its Python venv package and USB device
permissions. Do not use `sudo pip` or bypass an externally-managed-Python error.
If installation tries to compile HID support, use a Python/platform combination
with a supported hidapi wheel rather than requiring beginners to install a compiler.

These commands require access to the release source. Private repositories need
authentication; do not paste credentials into shared commands.

## Contributor and release maintenance

Contributors can install from the repository root with
`python -m pip install -e .` and run
`python -m unittest discover -s tests -v`.

The maintainer creates and publishes releases manually in GitHub. Publishing a
release (including a prerelease) triggers the executable workflow on its commit:
install, offline tests, PyInstaller build, frozen CLI smoke checks, hash, upload.
Saving a draft does not trigger it. The workflow creates neither tags nor releases.
Existing assets are not overwritten. The Windows runner builds a console
application with Python/HID support, not ADB or firmware images.
The workflow must be present in the released source.

# Locero Windows Helper installer

This directory contains the source used to build `Locero-Windows-Helper.exe`.

The installer is a native Windows bootstrapper. It installs Locero's native-messaging helper under the current user's Local AppData directory, registers the Firefox Native Messaging Host under HKCU, and keeps helper versions isolated so upgrades or repairs do not overwrite a running process.

Locero's helper itself is implemented in `native/host.py`. The installer bundles that source and a small native launcher. On first installation it downloads the official 64-bit Python 3.13.13 embeddable runtime from `python.org` and keeps that private runtime inside Locero's helper directory. Users therefore do not need to install Python themselves.

The installer does not require administrator privileges.

## Automatic Locero library

On a genuinely new install, the helper installer creates `%USERPROFILE%\Locero`. Archived HTML/PDF files live directly in the selected Locero folder, while the active SQLite catalogue, recovery data, and undo/history data live in its `.locero-data` subfolder. Machine-specific configuration and protected WebDAV credentials remain in Local AppData. Optional Cloud Backup is handled directly by the native helper over WebDAV.

v0.10.141 standardises the visible archive at `%USERPROFILE%\Locero`. Existing archive data is copied and verified before Locero switches paths; old source folders are left in place. It also safely recovers files misplaced in `%USERPROFILE%\Locero\.locero-data\archive` by v0.10.140 without deleting that recovery copy.

## Uninstall

The installer registers **Locero Windows Helper** in the current user's Windows **Installed apps** list and stores a small uninstaller copy under `%LOCALAPPDATA%\Locero\Installer`. Users can uninstall either from Windows Settings or from Locero's **Settings → Configuration → Uninstall Helper…** action.

Uninstall removes the Firefox native-messaging registration, Locero's private helper binaries, and its private embedded Python runtime. It deliberately does **not** delete the user's Locero library, archived webpages/PDFs, `%USERPROFILE%\Locero\.locero-data`, or backup directory. Firefox does not need to close.

## Build

Run `build.ps1` on Windows with Go 1.23+ installed. The resulting stable release asset is named:

`Locero-Windows-Helper.exe`


The native launcher starts its private Python runtime with no visible console window while preserving Firefox native-messaging stdin/stdout. Each installer run uses a unique helper instance directory, so reinstalling the same Locero version is safe while Firefox is open.

## Live helper updates

The helper is installed side-by-side under `%LOCALAPPDATA%\Locero\NativeHelper`.
Updating Locero does not overwrite the helper executable currently in use by
Firefox. The installer writes a new versioned instance, switches the native
messaging registry entry atomically, and retires the previous Locero helper.
Firefox and existing tabs do not need to be closed.

From v0.10.114 onward, installed helpers also watch the `ACTIVE_HOST` marker and
exit automatically when a newer helper becomes active. This makes subsequent
updates a clean native-messaging handover rather than an in-place replacement.

## Windows application manifest

`build.ps1` builds both Go GUI executables and then runs the dependency-free `manifestpatch` build helper. It embeds the XML application manifests in `installer.manifest` and `launcher.manifest`. The manifests explicitly request `asInvoker` and declare modern Windows compatibility so the helper does not rely on legacy Windows installer heuristics.

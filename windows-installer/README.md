# StashLibrary Windows Helper installer

This directory contains the source used to build the StashLibrary Windows helper, distributed as `StashLibrary-Windows-Helper.exe`.

The installer is a native Windows bootstrapper. It installs StashLibrary's native-messaging helper under the current user's Local AppData directory, registers the Firefox Native Messaging Host under HKCU, and keeps helper versions isolated so upgrades or repairs do not overwrite a running process.

StashLibrary's helper itself is implemented in `native/host.py`. The installer bundles that source and a small native launcher. On first installation it downloads the official 64-bit Python 3.13.13 embeddable runtime from `python.org` and keeps that private runtime inside StashLibrary's compatibility directory. Users therefore do not need to install Python themselves.

The installer does not require administrator privileges.

## Automatic StashLibrary library

On a genuinely new install, the helper installer creates `StashLibrary` at the root of the current user's Windows drive (normally `C:\StashLibrary`). Archived HTML/PDF files live directly in the selected StashLibrary storage folder, while the active SQLite catalogue, recovery data, and undo/history data live in its `.stashlibrary-data` subfolder. Machine-specific configuration and protected WebDAV credentials remain in Local AppData. Optional Cloud Backup is handled directly by the native helper over WebDAV. Existing legacy storage is copied and verified before the app switches to this folder.

v0.10.141 standardised the legacy visible archive at `%USERPROFILE%\StashLibrary`. Existing archive data is copied and verified before StashLibrary switches paths; old source folders are left in place. It also safely recovers files misplaced in `%USERPROFILE%\StashLibrary\.stashlibrary-data\archive` by v0.10.140 without deleting that recovery copy.

## Uninstall

The installer registers **StashLibrary Windows Helper** in the current user's Windows **Installed apps** list and stores a small uninstaller copy under the legacy `%LOCALAPPDATA%\StashLibrary\Installer` compatibility path. Users can uninstall either from Windows Settings or from StashLibrary's uninstall screen.

Uninstall removes the Firefox native-messaging registration, StashLibrary's private helper binaries, and its private embedded Python runtime. It deliberately does **not** delete the user's library, archived webpages/PDFs, legacy `%USERPROFILE%\StashLibrary\.stashlibrary-data`, or backup directory. Firefox does not need to close.

## Build

Run `build.ps1` on Windows with Go 1.23+ installed. The resulting stable release asset is named:

`StashLibrary-Windows-Helper.exe`


The native launcher starts its private Python runtime with no visible console window while preserving Firefox native-messaging stdin/stdout. Each installer run uses a unique helper instance directory, so reinstalling the same StashLibrary version is safe while Firefox is open.

## Live helper updates

The helper is installed side-by-side under `%LOCALAPPDATA%\StashLibrary\NativeHelper`.
Updating StashLibrary does not overwrite the helper executable currently in use by
Firefox. The installer writes a new versioned instance, switches the native
messaging registry entry atomically, and retires the previous StashLibrary helper.
Firefox and existing tabs do not need to be closed.

From v0.10.114 onward, installed helpers also watch the `ACTIVE_HOST` marker and
exit automatically when a newer helper becomes active. This makes subsequent
updates a clean native-messaging handover rather than an in-place replacement.

## Windows application manifest

`build.ps1` builds both Go GUI executables and then runs the dependency-free `manifestpatch` build helper. It embeds the XML application manifests in `installer.manifest` and `launcher.manifest`. The manifests explicitly request `asInvoker` and declare modern Windows compatibility so the helper does not rely on legacy Windows installer heuristics.

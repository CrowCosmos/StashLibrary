package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	stashLibraryVersion = "0.1.1"
	pythonURL           = "https://www.python.org/ftp/python/3.13.13/python-3.13.13-embed-amd64.zip"
	pythonSHA256        = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"
	nativeHostName      = "stashlibrary.host"
	extensionID         = "stashlibrary@stashlibrary.app"
)

//go:embed payload/StashLibraryNativeHelper.exe payload/host.py
var payload embed.FS

func messageBox(title, text string, flags uintptr) {
	user32 := syscall.NewLazyDLL("user32.dll")
	proc := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	proc.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), flags)
}

func runReg(args ...string) error {
	cmd := exec.Command("reg.exe", args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("reg.exe failed: %w (%s)", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func stashLibraryRoot() (string, error) {
	lad := os.Getenv("LOCALAPPDATA")
	if lad == "" {
		return "", fmt.Errorf("LOCALAPPDATA is not available")
	}
	return filepath.Join(lad, "StashLibrary"), nil
}

var stashLibraryPath string
var stashLibraryCreated bool

// ensureStashLibrary gives first-time users a working local archive without
// making them choose a folder before they can try StashLibrary. The selected
// StashLibrary folder contains ordinary saved HTML/PDF files and may itself
// live inside a desktop cloud-sync folder. Catalogue, recovery and undo/history data live with the archive in a .stashlibrary-data
// folder. Machine-specific configuration and cloud credentials remain in LocalAppData.
func ensureStashLibrary() (string, bool, error) {
	lad := os.Getenv("LOCALAPPDATA")
	if lad == "" {
		return "", false, fmt.Errorf("LOCALAPPDATA is not available")
	}
	dataDir := filepath.Join(lad, "StashLibrary", "Data")
	configPath := filepath.Join(dataDir, "config.json")
	previousConfigPath := filepath.Join(lad, "Lo"+"cero", "Data", "config.json")
	legacyConfigPath := filepath.Join(lad, "LocalFileBookmarks", "config.json")
	if err := os.MkdirAll(dataDir, 0755); err != nil {
		return "", false, err
	}

	cfg := map[string]any{}
	loadedLegacy := false
	readConfig := func(path string) error {
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if len(bytes.TrimSpace(data)) > 0 {
			if err := json.Unmarshal(data, &cfg); err != nil {
				return fmt.Errorf("could not read existing StashLibrary configuration: %w", err)
			}
		}
		return nil
	}
	if err := readConfig(configPath); err != nil {
		if !os.IsNotExist(err) {
			return "", false, err
		}
		if err := readConfig(previousConfigPath); err == nil {
			loadedLegacy = true
		} else if !os.IsNotExist(err) {
			return "", false, err
		} else if err := readConfig(legacyConfigPath); err == nil {
			loadedLegacy = true
		} else if !os.IsNotExist(err) {
			return "", false, err
		}
	}

	if existing, ok := cfg["bookmarks_path"].(string); ok && strings.TrimSpace(existing) != "" {
		// Preserve the configured StashLibrary storage folder during installation. The native host
		// handles any safe layout migration on its next start.
		if _, set := cfg["internal_data_layout"]; !set {
			cfg["internal_data_layout"] = "localappdata-v1"
		}
		if loadedLegacy {
			cfg["config_migrated_from"] = legacyConfigPath
		}
		b, err := json.MarshalIndent(cfg, "", "  ")
		if err != nil {
			return "", false, err
		}
		if err := os.WriteFile(configPath, b, 0644); err != nil {
			return "", false, fmt.Errorf("could not save StashLibrary configuration: %w", err)
		}
		return existing, false, nil
	}

	home, err := os.UserHomeDir()
	if err != nil || strings.TrimSpace(home) == "" {
		return "", false, fmt.Errorf("could not determine the current user's home folder")
	}
	homeVolume := filepath.VolumeName(home)
	library := filepath.Join(home, "StashLibrary")
	if homeVolume != "" {
		library = filepath.Join(homeVolume+string(os.PathSeparator), "StashLibrary")
	}
	if err := os.MkdirAll(library, 0755); err != nil {
		return "", false, fmt.Errorf("could not create the default StashLibrary storage folder: %w", err)
	}
	cfg["bookmarks_path"] = library
	cfg["cloud_sync_path"] = library
	cfg["folder_config_version"] = 9
	cfg["storage_layout"] = "flat-sqlite-v2"
	cfg["internal_data_layout"] = "library-local-v3"
	cfg["internal_data_path"] = filepath.Join(library, ".stashlibrary-data")
	if err := os.MkdirAll(filepath.Join(library, ".stashlibrary-data"), 0755); err != nil {
		return "", false, fmt.Errorf("could not create StashLibrary data folder: %w", err)
	}
	cfg["cloud_sync_enabled"] = false
	cfg["cloud_sync_model"] = "retired-v0.10.169"
	cfg["storage_root_layout"] = "self-contained-v1"
	cfg["storage_sync_mode"] = "local"
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", false, err
	}
	if err := os.WriteFile(configPath, b, 0644); err != nil {
		return "", false, fmt.Errorf("could not save the default StashLibrary storage folder: %w", err)
	}
	return library, true, nil
}

func writeEmbedded(name, dst string) error {
	b, err := payload.ReadFile(name)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, b, 0755)
}

func download(url string) ([]byte, error) {
	client := &http.Client{Timeout: 3 * time.Minute}
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", "StashLibrary-Windows-Helper/"+stashLibraryVersion)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download returned %s", resp.Status)
	}
	return io.ReadAll(resp.Body)
}

func verifySHA256(data []byte, expected string) error {
	sum := sha256.Sum256(data)
	got := hex.EncodeToString(sum[:])
	if !strings.EqualFold(got, expected) {
		return fmt.Errorf("downloaded Python runtime failed SHA-256 verification")
	}
	return nil
}

func unzipBytes(data []byte, dst string) error {
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return err
	}
	for _, f := range zr.File {
		clean := filepath.Clean(f.Name)
		if filepath.IsAbs(clean) || strings.HasPrefix(clean, "..") {
			return fmt.Errorf("unsafe archive path: %s", f.Name)
		}
		path := filepath.Join(dst, clean)
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(path, 0755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			return err
		}
		in, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.Create(path)
		if err != nil {
			in.Close()
			return err
		}
		_, cpErr := io.Copy(out, in)
		closeErr := out.Close()
		in.Close()
		if cpErr != nil {
			return cpErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		return err
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	_, cpErr := io.Copy(out, in)
	closeErr := out.Close()
	if cpErr != nil {
		return cpErr
	}
	return closeErr
}

func registerInstalledApp(root string) error {
	installerDir := filepath.Join(root, "Installer")
	stableExe := filepath.Join(installerDir, "StashLibrary-Windows-Helper.exe")
	self, err := os.Executable()
	if err != nil {
		return err
	}
	selfAbs, _ := filepath.Abs(self)
	stableAbs, _ := filepath.Abs(stableExe)
	if !strings.EqualFold(filepath.Clean(selfAbs), filepath.Clean(stableAbs)) {
		if err := copyFile(selfAbs, stableAbs); err != nil {
			return fmt.Errorf("could not install the Windows uninstaller: %w", err)
		}
	}

	key := `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\StashLibraryWindowsHelper`
	values := [][]string{
		{"ADD", key, "/v", "DisplayName", "/t", "REG_SZ", "/d", "StashLibrary Windows Helper", "/f"},
		{"ADD", key, "/v", "DisplayVersion", "/t", "REG_SZ", "/d", stashLibraryVersion, "/f"},
		{"ADD", key, "/v", "Publisher", "/t", "REG_SZ", "/d", "StashLibrary", "/f"},
		{"ADD", key, "/v", "InstallLocation", "/t", "REG_SZ", "/d", root, "/f"},
		{"ADD", key, "/v", "DisplayIcon", "/t", "REG_SZ", "/d", stableExe, "/f"},
		{"ADD", key, "/v", "UninstallString", "/t", "REG_SZ", "/d", `"` + stableExe + `" --uninstall`, "/f"},
		{"ADD", key, "/v", "QuietUninstallString", "/t", "REG_SZ", "/d", `"` + stableExe + `" --uninstall --quiet`, "/f"},
		{"ADD", key, "/v", "NoModify", "/t", "REG_DWORD", "/d", "1", "/f"},
		{"ADD", key, "/v", "NoRepair", "/t", "REG_DWORD", "/d", "0", "/f"},
	}
	for _, args := range values {
		if err := runReg(args...); err != nil {
			return err
		}
	}
	return nil
}

func deleteRegKeyBestEffort(key string) {
	cmd := exec.Command("reg.exe", "DELETE", key, "/f")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Run()
}

func scheduleInstallerSelfCleanup(root string) {
	installerDir := filepath.Join(root, "Installer")
	quotedInstaller := `"` + installerDir + `"`
	quotedRoot := `"` + root + `"`
	command := `ping 127.0.0.1 -n 3 >nul & rmdir /s /q ` + quotedInstaller + ` & rmdir ` + quotedRoot + ` 2>nul`
	cmd := exec.Command("cmd.exe", "/d", "/c", command)
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	_ = cmd.Start()
}

func uninstallHelper() error {
	root, err := stashLibraryRoot()
	if err != nil {
		return err
	}
	helperRoot := filepath.Join(root, "NativeHelper")
	pyDir := filepath.Join(root, "Runtime", "python-3.13.13")
	_ = os.WriteFile(filepath.Join(helperRoot, "ACTIVE_HOST"), []byte("__STASHLIBRARY_UNINSTALLING__\r\n"), 0644)

	deleteRegKeyBestEffort(`HKCU\Software\Mozilla\NativeMessagingHosts\` + nativeHostName)
	deleteRegKeyBestEffort(`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\StashLibraryWindowsHelper`)

	// Give current native hosts a brief chance to notice ACTIVE_HOST, then stop
	// only StashLibrary-owned helper/embedded-Python processes. Firefox itself and all
	// open tabs stay running.
	time.Sleep(250 * time.Millisecond)
	retireOldStashLibraryHelpers(helperRoot, pyDir, filepath.Join(helperRoot, "__keep_nothing__"))
	time.Sleep(220 * time.Millisecond)
	retireOldStashLibraryHelpers(helperRoot, pyDir, filepath.Join(helperRoot, "__keep_nothing__"))

	for i := 0; i < 8; i++ {
		_ = os.RemoveAll(helperRoot)
		_ = os.RemoveAll(filepath.Join(root, "Runtime"))
		if _, e1 := os.Stat(helperRoot); os.IsNotExist(e1) {
			if _, e2 := os.Stat(filepath.Join(root, "Runtime")); os.IsNotExist(e2) {
				break
			}
		}
		time.Sleep(180 * time.Millisecond)
	}

	// Do not touch the user's StashLibrary data directory, archive,
	// configuration, or backup directory. Installer/helper/runtime files are
	// removed; the non-recursive root cleanup naturally leaves Data in place.
	scheduleInstallerSelfCleanup(root)
	return nil
}

func install() error {
	root, err := stashLibraryRoot()
	if err != nil {
		return err
	}
	// First-time setup is automatic: create a normal local StashLibrary library.
	// Updates preserve any existing/custom library path exactly as configured.
	stashLibraryPath, stashLibraryCreated, err = ensureStashLibrary()
	if err != nil {
		return err
	}
	helperRoot := filepath.Join(root, "NativeHelper")
	// Install each run into a unique instance directory. This makes reinstalls
	// and same-version repairs safe even if Firefox still has the previous
	// native helper executable open. The helper reports the logical StashLibrary
	// version from host.py, so the instance suffix is purely an install detail.
	instanceID := time.Now().UTC().Format("20060102T150405.000000000")
	versionDir := filepath.Join(helperRoot, stashLibraryVersion+"-"+instanceID)
	pyDir := filepath.Join(root, "Runtime", "python-3.13.13")
	if err := os.MkdirAll(versionDir, 0755); err != nil {
		return err
	}
	if err := os.MkdirAll(pyDir, 0755); err != nil {
		return err
	}

	// A unique helper directory means the installer never overwrites a running
	// native host. This works for both upgrades and reinstalling the same version.
	if err := writeEmbedded("payload/StashLibraryNativeHelper.exe", filepath.Join(versionDir, "StashLibraryNativeHelper.exe")); err != nil {
		return err
	}
	if err := writeEmbedded("payload/host.py", filepath.Join(versionDir, "host.py")); err != nil {
		return err
	}

	// The embedded Python runtime is shared across helper versions. Download it
	// only on the first install (or if the runtime was removed).
	pythonExe := filepath.Join(pyDir, "python.exe")
	if _, statErr := os.Stat(pythonExe); statErr != nil {
		pyZip, err := download(pythonURL)
		if err != nil {
			return fmt.Errorf("could not download Python runtime from python.org: %w", err)
		}
		if err := verifySHA256(pyZip, pythonSHA256); err != nil {
			return err
		}
		if err := unzipBytes(pyZip, pyDir); err != nil {
			return fmt.Errorf("could not unpack Python runtime: %w", err)
		}
	}

	manifest := map[string]any{
		"name":               nativeHostName,
		"description":        "Native filesystem helper for StashLibrary",
		"path":               filepath.Join(versionDir, "StashLibraryNativeHelper.exe"),
		"type":               "stdio",
		"allowed_extensions": []string{extensionID},
	}
	b, _ := json.MarshalIndent(manifest, "", "  ")
	manifestPath := filepath.Join(versionDir, nativeHostName+".json")
	if err := os.WriteFile(manifestPath, b, 0644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(versionDir, "VERSION"), []byte(stashLibraryVersion+"\r\n"), 0644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(helperRoot, "CURRENT"), []byte(stashLibraryVersion+"\r\n"), 0644); err != nil {
		return err
	}

	// Switching this one HKCU value is the atomic update operation. Existing
	// Firefox tabs stay open; only the native-messaging process is handed over.
	if err := runReg("ADD", `HKCU\Software\Mozilla\NativeMessagingHosts\`+nativeHostName, "/ve", "/t", "REG_SZ", "/d", manifestPath, "/f"); err != nil {
		return err
	}

	// Tell current/future StashLibrary helpers which side-by-side installation is active.
	// v0.10.119+ helpers watch this marker and exit gracefully when superseded.
	if err := os.WriteFile(filepath.Join(helperRoot, "ACTIVE_HOST"), []byte(versionDir+"\r\n"), 0644); err != nil {
		return err
	}

	// Older helpers did not have the watchdog. Retire only StashLibrary-owned helper
	// processes after the registry target has already switched to the new build.
	// This is best-effort: the Firefox extension also disconnects its current port
	// before starting an update and reconnects to the newly registered host.
	time.Sleep(180 * time.Millisecond)
	retireOldStashLibraryHelpers(helperRoot, pyDir, versionDir)
	time.Sleep(220 * time.Millisecond)
	retireOldStashLibraryHelpers(helperRoot, pyDir, versionDir)

	// Keep a copy in LOCALAPPDATA and register it with Windows Installed Apps so
	// users can remove the helper even if they later uninstall the Firefox add-on.
	if err := registerInstalledApp(root); err != nil {
		return err
	}
	return nil
}

func main() {
	args := os.Args[1:]
	uninstall := false
	quiet := false
	for _, a := range args {
		switch strings.ToLower(strings.TrimSpace(a)) {
		case "--uninstall", "/uninstall":
			uninstall = true
		case "--quiet", "/quiet":
			quiet = true
		}
	}
	if uninstall {
		if err := uninstallHelper(); err != nil {
			if !quiet {
				messageBox("StashLibrary Windows Helper", "Uninstall failed:\n\n"+err.Error(), 0x10)
			}
			os.Exit(1)
		}
		if !quiet {
			messageBox("StashLibrary Windows Helper", "StashLibrary Windows Helper was uninstalled.\n\nYour StashLibrary library, saved webpages/PDFs, settings, and backups were kept.", 0x40)
		}
		return
	}

	if err := install(); err != nil {
		messageBox("StashLibrary Windows Helper", "Installation failed:\n\n"+err.Error()+"\n\nAn internet connection is required the first time so the installer can obtain the official Python runtime from python.org.", 0x10)
		os.Exit(1)
	}
	msg := "StashLibrary Windows Helper is installed."
	if stashLibraryPath != "" {
		if stashLibraryCreated {
			msg += "\n\nYour StashLibrary storage folder was created automatically at:\n" + stashLibraryPath
		} else {
			msg += "\n\nYour existing StashLibrary storage folder was kept:\n" + stashLibraryPath
		}
	}
	msg += "\n\nReturn to Firefox to continue with Zotero setup."
	messageBox("StashLibrary Windows Helper", msg, 0x40)
}

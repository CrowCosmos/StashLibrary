//go:build windows

package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

const (
	createNoWindow      = 0x08000000
	startfUseStdHandles = 0x00000100
	infinite            = 0xFFFFFFFF
)

var (
	kernel32Launcher        = syscall.NewLazyDLL("kernel32.dll")
	procCreateProcessW      = kernel32Launcher.NewProc("CreateProcessW")
	procWaitForSingleObject = kernel32Launcher.NewProc("WaitForSingleObject")
	procGetExitCodeProcess  = kernel32Launcher.NewProc("GetExitCodeProcess")
	procCloseHandleLauncher = kernel32Launcher.NewProc("CloseHandle")
)

// launchHiddenPython starts Locero's private Python runtime without allocating a
// console window, while explicitly inheriting Firefox native-messaging stdin,
// stdout and stderr. Using CreateProcessW directly makes the no-console contract
// unambiguous even though python.exe itself is a console-subsystem executable.
func launchHiddenPython(python, host, workingDir string) int {
	commandLine := fmt.Sprintf("\"%s\" -u \"%s\"", python, host)
	cmdUTF16, err := syscall.UTF16FromString(commandLine)
	if err != nil {
		return 1
	}
	appUTF16, err := syscall.UTF16PtrFromString(python)
	if err != nil {
		return 1
	}
	dirUTF16, err := syscall.UTF16PtrFromString(workingDir)
	if err != nil {
		return 1
	}

	var si syscall.StartupInfo
	si.Cb = uint32(unsafe.Sizeof(si))
	si.Flags = startfUseStdHandles
	si.StdInput = syscall.Handle(os.Stdin.Fd())
	si.StdOutput = syscall.Handle(os.Stdout.Fd())
	si.StdErr = syscall.Handle(os.Stderr.Fd())

	var pi syscall.ProcessInformation
	r1, _, _ := procCreateProcessW.Call(
		uintptr(unsafe.Pointer(appUTF16)),
		uintptr(unsafe.Pointer(&cmdUTF16[0])),
		0,
		0,
		1, // inherit native-messaging pipe handles
		createNoWindow,
		0,
		uintptr(unsafe.Pointer(dirUTF16)),
		uintptr(unsafe.Pointer(&si)),
		uintptr(unsafe.Pointer(&pi)),
	)
	if r1 == 0 {
		return 1
	}
	if pi.Thread != 0 {
		procCloseHandleLauncher.Call(uintptr(pi.Thread))
	}
	if pi.Process == 0 {
		return 1
	}
	defer procCloseHandleLauncher.Call(uintptr(pi.Process))

	procWaitForSingleObject.Call(uintptr(pi.Process), infinite)
	var exitCode uint32
	ok, _, _ := procGetExitCodeProcess.Call(uintptr(pi.Process), uintptr(unsafe.Pointer(&exitCode)))
	if ok == 0 {
		return 1
	}
	return int(exitCode)
}

func main() {
	exe, err := os.Executable()
	if err != nil {
		os.Exit(1)
	}
	dir := filepath.Dir(exe)
	appRoot := filepath.Dir(filepath.Dir(dir))
	python := filepath.Join(appRoot, "Runtime", "python-3.13.13", "python.exe")
	host := filepath.Join(dir, "host.py")

	// Refuse unexpected paths instead of falling back through cmd.exe/batch
	// launchers, which could expose a console window.
	if !strings.EqualFold(filepath.Ext(python), ".exe") {
		os.Exit(1)
	}
	os.Exit(launchHiddenPython(python, host, dir))
}

//go:build windows

package main

import (
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

const (
	th32csSnapProcess              = 0x00000002
	processTerminate               = 0x0001
	processQueryLimitedInformation = 0x1000
)

type processEntry32 struct {
	Size            uint32
	CntUsage        uint32
	ProcessID       uint32
	DefaultHeapID   uintptr
	ModuleID        uint32
	CntThreads      uint32
	ParentProcessID uint32
	PriClassBase    int32
	Flags           uint32
	ExeFile         [260]uint16
}

var (
	kernel32                       = syscall.NewLazyDLL("kernel32.dll")
	procCreateToolhelp32Snapshot   = kernel32.NewProc("CreateToolhelp32Snapshot")
	procProcess32FirstW            = kernel32.NewProc("Process32FirstW")
	procProcess32NextW             = kernel32.NewProc("Process32NextW")
	procOpenProcess                = kernel32.NewProc("OpenProcess")
	procQueryFullProcessImageNameW = kernel32.NewProc("QueryFullProcessImageNameW")
	procTerminateProcess           = kernel32.NewProc("TerminateProcess")
	procCloseHandle                = kernel32.NewProc("CloseHandle")
)

func cleanFoldPath(p string) string {
	return strings.ToLower(filepath.Clean(strings.TrimSpace(p)))
}

func processImagePath(pid uint32) string {
	h, _, _ := procOpenProcess.Call(processQueryLimitedInformation, 0, uintptr(pid))
	if h == 0 {
		return ""
	}
	defer procCloseHandle.Call(h)
	buf := make([]uint16, 32768)
	n := uint32(len(buf))
	ok, _, _ := procQueryFullProcessImageNameW.Call(h, 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&n)))
	if ok == 0 || n == 0 {
		return ""
	}
	return syscall.UTF16ToString(buf[:n])
}

func terminatePID(pid uint32) {
	h, _, _ := procOpenProcess.Call(processTerminate, 0, uintptr(pid))
	if h == 0 {
		return
	}
	defer procCloseHandle.Call(h)
	procTerminateProcess.Call(h, 0)
}

// retireOldStashLibraryHelpers stops only processes belonging to StashLibrary's private
// native-helper installation. It never targets system Python or arbitrary apps.
func retireOldStashLibraryHelpers(helperRoot, pythonDir, keepDir string) {
	snap, _, _ := procCreateToolhelp32Snapshot.Call(th32csSnapProcess, 0)
	if snap == 0 || snap == ^uintptr(0) {
		return
	}
	defer procCloseHandle.Call(snap)

	helperPrefix := cleanFoldPath(helperRoot) + string(filepath.Separator)
	keepPrefix := cleanFoldPath(keepDir) + string(filepath.Separator)
	privatePython := cleanFoldPath(filepath.Join(pythonDir, "python.exe"))

	var pe processEntry32
	pe.Size = uint32(unsafe.Sizeof(pe))
	ok, _, _ := procProcess32FirstW.Call(snap, uintptr(unsafe.Pointer(&pe)))
	for ok != 0 {
		if pe.ProcessID != 0 {
			image := processImagePath(pe.ProcessID)
			folded := cleanFoldPath(image)
			if folded != "" {
				base := strings.ToLower(filepath.Base(folded))
				oldLauncherName := "lo" + "ceronativehelper.exe"
				oldLauncher := base == oldLauncherName && strings.HasPrefix(folded, helperPrefix) && !strings.HasPrefix(folded, keepPrefix)
				stashLibraryPython := folded == privatePython
				if oldLauncher || stashLibraryPython {
					terminatePID(pe.ProcessID)
				}
			}
		}
		pe.Size = uint32(unsafe.Sizeof(pe))
		ok, _, _ = procProcess32NextW.Call(snap, uintptr(unsafe.Pointer(&pe)))
	}
}

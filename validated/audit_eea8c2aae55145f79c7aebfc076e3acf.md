### Title
Out-of-bounds read in Windows reparse-point symlink parsing due to unvalidated offset/length fields - ([File: src/internal/syscall/windows/reparse_windows.go])

### Summary
`SymbolicLinkReparseBuffer.Path()` and `MountPointReparseBuffer.Path()` compute a substring of the reparse `PathBuffer` using `SubstituteNameOffset`/`SubstituteNameLength` fields taken directly from the on-disk/DeviceIoControl-returned reparse-point structure, without validating them against the actual size of the buffer that was read (`syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE`, 16 KiB). This mirrors the Linux SMB client `symlink_data()` bug class: an attacker-controlled length field is used to compute a pointer/slice range that can extend past the real allocation, causing an out-of-bounds read.

### Finding Description
`readReparseLinkHandle` in `src/os/file_windows.go:476-499` allocates a 16 KiB buffer (`rdbbuf := make([]byte, syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE)`) and fills it via `syscall.DeviceIoControl(h, syscall.FSCTL_GET_REPARSE_POINT, ...)`, then casts the raw bytes to `*windows.REPARSE_DATA_BUFFER` and, for `IO_REPARSE_TAG_SYMLINK`/`IO_REPARSE_TAG_MOUNT_POINT`, to `*windows.SymbolicLinkReparseBuffer` / `*windows.MountPointReparseBuffer` [1](#0-0) .

`Path()` then does:
```go
n1 := rb.SubstituteNameOffset / 2
n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
``` [2](#0-1) 

The unsafe cast reinterprets the memory at `PathBuffer[0]` as a `[0xffff]uint16` array (131,070 bytes), and Go's bounds checking for the slice expression `[n1:n2:n2]` is only checked against that *declared* array length (65535 elements), not against the real backing allocation (the 16 KiB `rdbbuf`). `SubstituteNameOffset`/`SubstituteNameLength` are attacker-controlled `uint16` values taken straight from the reparse-point data returned by the kernel for the file being inspected; nothing validates them against `ReparseDataLength` or the real buffer size before use [3](#0-2) . The identical unchecked pattern also exists in `src/syscall/syscall_windows.go`'s `Readlink`, which slices a `(*[0xffff]uint16)` view of the `PathBuffer` using the same unvalidated offset/length fields [4](#0-3) .

Because `n1`/`n2` can be as large as `0xffff` while the actual buffer is only 16 KiB (`0x4000` bytes / 8192 `uint16` elements), an attacker who can plant a maliciously crafted reparse point (e.g., on a removable drive, mounted image/ISO, or a filesystem entry visible to a victim's file-walking code) can make `SubstituteNameOffset + SubstituteNameLength` exceed the true buffer bounds, causing `UTF16ToString` to read memory well past the 16 KiB `rdbbuf` allocation — an out-of-bounds heap read whose contents get decoded into the returned "symlink target" string (a memory-disclosure primitive), or a crash if the read strays into unmapped memory.

### Impact Explanation
This is an out-of-bounds heap read reachable from `os.Readlink`, `os.Lstat`/`os.Stat` path resolution, and `filepath.Walk`-style directory traversal on Windows whenever a directory contains a reparse point (symlink/junction) with attacker-influenced `SubstituteNameOffset`/`SubstituteNameLength` fields. The impact is potential disclosure of adjacent heap memory (leaked into the decoded "link target" string returned to the caller) or a process crash — matching the "C:H"/oob-read character of the reported kernel CVE, though on the Go side the reachable input is filesystem/reparse-point data rather than an SMB server response.

### Likelihood Explanation
Exploitation requires an attacker able to place a crafted reparse point where a victim Go program will call `os.Readlink`/`os.Lstat`/directory-walk on it (e.g., a shared folder, downloaded/extracted archive, mounted removable media, or a Root-scoped directory traversal). Creating fully arbitrary reparse-point byte content typically requires either `SeCreateSymbolicLinkPrivilege`/Developer Mode or direct low-level tooling (e.g., `fsutil reparsepoint`) — this narrows the practical attacker population but does not eliminate it, since unprivileged users can create symlinks in Developer Mode and various tools can write raw reparse buffers to files they own.

### Recommendation
Validate `SubstituteNameOffset`, `SubstituteNameLength`, `PrintNameOffset`, and `PrintNameLength` against the actual number of bytes returned by `DeviceIoControl` (`bytesReturned`) and against `ReparseDataLength` before using them to slice `PathBuffer`, and bound the temporary array cast to the real buffer size (e.g., using `unsafe.Slice` sized to the actual returned byte count) instead of a fixed `[0xffff]uint16` cast that bypasses the real allocation size in Go's bounds checking.

### Proof of Concept
```go
package windows_test

import (
    "syscall"
    "unsafe"

    "internal/syscall/windows"
)

// Simulates a 16KiB reparse buffer (as read by os/file_windows.go)
// with an attacker-controlled SubstituteNameOffset/Length that exceeds
// the real allocation, but stays within 0xffff (65535) uint16 elements.
func TestSymbolicLinkReparseBuffer_OOBRead(t *testing.T) {
    const bufSize = 16 * 1024 // syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE
    raw := make([]byte, bufSize)

    rb := (*windows.SymbolicLinkReparseBuffer)(unsafe.Pointer(&raw[0]))
    // Attacker-controlled fields taken "from disk":
    rb.SubstituteNameOffset = 0
    rb.SubstituteNameLength = 0xfffe // offset+len/2 ~= 0x7fff elements = 65534*2 bytes,
                                      // far beyond the 16KiB (8192-element) real buffer.

    // This performs an OOB read past `raw` because Path() casts to a
    // fixed [0xffff]uint16 array irrespective of the real backing size.
    _ = rb.Path() // expected: bounds-checked failure or leaked adjacent memory,
                   // not a safe, in-bounds read.
}
```
Expected assertion: `Path()` should either return an error or be restricted so it can never read beyond `bufSize` bytes; instead it currently reads/decodes memory beyond the actual `raw` allocation whenever `SubstituteNameOffset+SubstituteNameLength` exceeds the true buffer size but stays under `0xffff*2` bytes.

### Citations

**File:** src/os/file_windows.go (L476-494)
```go
func readReparseLinkHandle(h syscall.Handle) (string, error) {
	rdbbuf := make([]byte, syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
	var bytesReturned uint32
	err := syscall.DeviceIoControl(h, syscall.FSCTL_GET_REPARSE_POINT, nil, 0, &rdbbuf[0], uint32(len(rdbbuf)), &bytesReturned, nil)
	if err != nil {
		return "", err
	}

	rdb := (*windows.REPARSE_DATA_BUFFER)(unsafe.Pointer(&rdbbuf[0]))
	switch rdb.ReparseTag {
	case syscall.IO_REPARSE_TAG_SYMLINK:
		rb := (*windows.SymbolicLinkReparseBuffer)(unsafe.Pointer(&rdb.DUMMYUNIONNAME))
		s := rb.Path()
		if rb.Flags&windows.SYMLINK_FLAG_RELATIVE != 0 {
			return s, nil
		}
		return normaliseLinkPath(s)
	case windows.IO_REPARSE_TAG_MOUNT_POINT:
		return normaliseLinkPath((*windows.MountPointReparseBuffer)(unsafe.Pointer(&rdb.DUMMYUNIONNAME)).Path())
```

**File:** src/internal/syscall/windows/reparse_windows.go (L45-70)
```go
type SymbolicLinkReparseBuffer struct {
	// The integer that contains the offset, in bytes,
	// of the substitute name string in the PathBuffer array,
	// computed as an offset from byte 0 of PathBuffer. Note that
	// this offset must be divided by 2 to get the array index.
	SubstituteNameOffset uint16
	// The integer that contains the length, in bytes, of the
	// substitute name string. If this string is null-terminated,
	// SubstituteNameLength does not include the Unicode null character.
	SubstituteNameLength uint16
	// PrintNameOffset is similar to SubstituteNameOffset.
	PrintNameOffset uint16
	// PrintNameLength is similar to SubstituteNameLength.
	PrintNameLength uint16
	// Flags specifies whether the substitute name is a full path name or
	// a path name relative to the directory containing the symbolic link.
	Flags      uint32
	PathBuffer [1]uint16
}

// Path returns path stored in rb.
func (rb *SymbolicLinkReparseBuffer) Path() string {
	n1 := rb.SubstituteNameOffset / 2
	n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
	return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
}
```

**File:** src/syscall/syscall_windows.go (L1421-1447)
```go
	rdb := (*reparseDataBuffer)(unsafe.Pointer(&rdbbuf[0]))
	var s string
	switch rdb.ReparseTag {
	case IO_REPARSE_TAG_SYMLINK:
		data := (*symbolicLinkReparseBuffer)(unsafe.Pointer(&rdb.reparseBuffer))
		p := (*[0xffff]uint16)(unsafe.Pointer(&data.PathBuffer[0]))
		s = UTF16ToString(p[data.SubstituteNameOffset/2 : (data.SubstituteNameOffset+data.SubstituteNameLength)/2])
		if data.Flags&_SYMLINK_FLAG_RELATIVE == 0 {
			if len(s) >= 4 && s[:4] == `\??\` {
				s = s[4:]
				switch {
				case len(s) >= 2 && s[1] == ':': // \??\C:\foo\bar
					// do nothing
				case len(s) >= 4 && s[:4] == `UNC\`: // \??\UNC\foo\bar
					s = `\\` + s[4:]
				default:
					// unexpected; do nothing
				}
			} else {
				// unexpected; do nothing
			}
		}
	case _IO_REPARSE_TAG_MOUNT_POINT:
		data := (*mountPointReparseBuffer)(unsafe.Pointer(&rdb.reparseBuffer))
		p := (*[0xffff]uint16)(unsafe.Pointer(&data.PathBuffer[0]))
		s = UTF16ToString(p[data.SubstituteNameOffset/2 : (data.SubstituteNameOffset+data.SubstituteNameLength)/2])
		if len(s) >= 4 && s[:4] == `\??\` { // \??\C:\foo\bar
```

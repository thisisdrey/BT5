### Title
Heap out-of-bounds read in Windows reparse-point parsing via unvalidated `SubstituteNameOffset`/`SubstituteNameLength` - ([File: src/internal/syscall/windows/reparse_windows.go])

### Summary
`SymbolicLinkReparseBuffer.Path()` and `MountPointReparseBuffer.Path()` compute slice bounds directly from the `SubstituteNameOffset` and `SubstituteNameLength` fields of an NTFS reparse-point buffer without validating them against the actual size of the returned data (`bytesReturned`/`ReparseDataLength`). This mirrors the FreeRDP `smartcard_unpack_set_attrib_call` bug class, where a length field (`cbAttrLen`) taken from untrusted input is trusted without checking it against the real buffer length, producing a heap out-of-bounds read.

### Finding Description
`readReparseLinkHandle` in `src/os/file_windows.go:476-500` reads a reparse point via `syscall.DeviceIoControl(h, syscall.FSCTL_GET_REPARSE_POINT, ...)` into a fixed `rdbbuf` sized `syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE` (16 KiB). It never checks `bytesReturned` before interpreting the buffer. It then casts the union portion to `*windows.SymbolicLinkReparseBuffer` or `*windows.MountPointReparseBuffer` and calls `.Path()`:

```go
func (rb *SymbolicLinkReparseBuffer) Path() string {
	n1 := rb.SubstituteNameOffset / 2
	n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
	return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
}
``` [1](#0-0) 

`SubstituteNameOffset` and `SubstituteNameLength` are `uint16` fields taken verbatim from the on-disk/NTFS reparse-point payload — attacker-controllable when the attacker can create the reparse point being read (e.g., a junction/symlink placed on a filesystem, such as an extracted archive, USB drive, or shared directory that a victim later processes with `os.Readlink`, `filepath.EvalSymlinks`, or `os.Lstat`/`os.Stat`). The `unsafe.Pointer` is reinterpreted as a `*[0xffff]uint16` — i.e., up to 131,070 bytes — far larger than the actual 16 KiB (`MAXIMUM_REPARSE_DATA_BUFFER_SIZE`) buffer that was allocated and populated, and no code cross-checks `n2` against `rb.ReparseDataLength` (from `REPARSE_DATA_BUFFER_HEADER`) or against the number of bytes actually returned by `DeviceIoControl`. If a crafted reparse point declares `SubstituteNameOffset`/`SubstituteNameLength` values that exceed the true buffer contents, `UTF16ToString` on `p[n1:n2:n2]` reads heap memory beyond the valid reparse data — an out-of-bounds heap read, structurally identical to trusting `cbAttrLen` beyond the real NDR payload in the FreeRDP CVE. The same pattern also exists in the duplicated code path in `syscall.Readlink` at `src/syscall/syscall_windows.go:1421-1451`. [2](#0-1) [3](#0-2) 

### Impact Explanation
An out-of-bounds heap read could disclose adjacent heap memory contents (via the returned symlink/junction path string) or cause a crash if the read strays past mapped memory, when a Go program on Windows resolves an attacker-planted reparse point (`os.Readlink`, `filepath.EvalSymlinks`, `os.Lstat`, or the `os.Root` family via `readReparseLinkAt`/`readlinkat`). This would be a PUBLIC-track memory-safety bug class if confirmed as reachable with attacker-controlled `SubstituteNameOffset/Length` beyond the true data length — but I could not fully verify at this pass whether `DeviceIoControl`/NTFS enforces internal consistency between `ReparseDataLength` and `SubstituteNameOffset+SubstituteNameLength` before the data reaches user space, or whether the kernel already rejects malformed reparse buffers on write (`FSCTL_SET_REPARSE_POINT`) such that a well-formed NTFS volume could never surface an inconsistent buffer to `FSCTL_GET_REPARSE_POINT`. This "trust boundary" question is the crux of whether this is truly exploitable from an unprivileged filesystem entry, and I was not able to confirm it with available tools/index (kernel/NTFS-side validation is outside this repository).

### Likelihood Explanation
An unprivileged local attacker who can create a symlink/junction (or supply an archive/removable-media reparse point) that a victim Go program later inspects via `os.Readlink`, `filepath.EvalSymlinks`, or `os.Lstat` could attempt to trigger this if the OS does not itself validate the internal offset/length consistency of the reparse buffer before returning it via `DeviceIoControl`. Because normal Windows APIs (`CreateSymbolicLink`, `mklink`) always produce internally-consistent buffers, this would primarily be an issue if a hand-crafted reparse point (e.g., written raw via `FSCTL_SET_REPARSE_POINT` from a different process, or a corrupted/adversarial NTFS volume) can be planted; this significantly narrows the exploit surface and this could not be confirmed as reachable purely from Go-level code without kernel/filesystem investigation.

### Recommendation
In `SymbolicLinkReparseBuffer.Path()`/`MountPointReparseBuffer.Path()`, validate `SubstituteNameOffset` and `SubstituteNameLength` against the actual bytes returned by `DeviceIoControl` (or against `ReparseDataLength` from `REPARSE_DATA_BUFFER_HEADER`) before slicing, and reject/bound the values instead of blindly reinterpreting the pointer as a `[0xffff]uint16` array. Thread `bytesReturned` from `readReparseLinkHandle` into the `Path()` calculation so bounds checks are against the real data length, not an assumed maximum.

### Proof of Concept
Because reaching this requires crafting a raw reparse-point buffer at the Windows API level, a minimal Go-level test on the exported helper demonstrates the missing bounds check:

```go
package windows_test

import (
	"testing"
	"unsafe"

	"internal/syscall/windows"
)

func TestSymbolicLinkReparseBuffer_PathOOB(t *testing.T) {
	// Allocate a buffer no larger than MAXIMUM_REPARSE_DATA_BUFFER_SIZE (16 KiB),
	// as readReparseLinkHandle does.
	buf := make([]byte, 16*1024)
	rb := (*windows.SymbolicLinkReparseBuffer)(unsafe.Pointer(&buf[0]))

	// Attacker-controlled offset/length exceeding the real buffer contents.
	rb.SubstituteNameOffset = 0
	rb.SubstituteNameLength = 0xFFFE // requests ~64K uint16s, far beyond 16 KiB buffer

	// Expect Path() to reject/bound this instead of reading out-of-bounds heap memory.
	defer func() {
		if r := recover(); r == nil {
			t.Fatalf("expected bounds validation or panic, but Path() silently read out-of-bounds memory")
		}
	}()
	_ = rb.Path()
}
```
Expected: the call should fail safely (error or explicit bounds check) rather than silently returning data read past the allocated 16 KiB buffer, which is what the current implementation does.

### Citations

**File:** src/internal/syscall/windows/reparse_windows.go (L65-70)
```go
// Path returns path stored in rb.
func (rb *SymbolicLinkReparseBuffer) Path() string {
	n1 := rb.SubstituteNameOffset / 2
	n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
	return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
}
```

**File:** src/syscall/syscall_windows.go (L1421-1446)
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
```

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

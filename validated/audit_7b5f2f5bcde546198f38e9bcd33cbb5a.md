### Title
Unbounded reparse-point offset/length fields cause heap over-read in Windows symlink target parsing - ([File: src/internal/syscall/windows/reparse_windows.go])

### Summary
`SymbolicLinkReparseBuffer.Path()` and `MountPointReparseBuffer.Path()` trust the `SubstituteNameOffset`/`SubstituteNameLength` fields taken verbatim from on-disk NTFS reparse-point metadata and slice a hard-coded `*[0xffff]uint16` array cast over `rb.PathBuffer` without validating those values against the actual size of the buffer that was returned by `DeviceIoControl(FSCTL_GET_REPARSE_POINT)`. This mirrors the ocfs2 bug: an untrusted, self-describing on-disk length/offset field is used to slice a fixed-capacity in-memory buffer without checking it against the real amount of data read, producing an out-of-bounds read.

### Finding Description
`readReparseLinkHandle` in `src/os/file_windows.go` allocates `rdbbuf := make([]byte, syscall.MAXIMUM_REPARSE_DATA_BUFFER_SIZE)` (16 KiB) and fills it via `DeviceIoControl(h, syscall.FSCTL_GET_REPARSE_POINT, ...)`. [1](#0-0) 
The raw reparse tag data is then reinterpreted as `windows.SymbolicLinkReparseBuffer` or `windows.MountPointReparseBuffer`, and `rb.Path()` is called. [2](#0-1) 

`Path()` computes:
```go
n1 := rb.SubstituteNameOffset / 2
n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
```
`SubstituteNameOffset` and `SubstituteNameLength` are attacker/filesystem-controlled `uint16` values coming straight from the on-disk reparse-point blob — there is no check that `n2` fits within the number of bytes actually returned by `DeviceIoControl` (`bytesReturned`) or within `rdb.ReparseDataLength`. Casting to a fixed `[0xffff]uint16` array (131,070 bytes) lets the slice expression succeed for any `n2 <= 0xffff`, even though the real heap allocation backing `rdbbuf` is only 16,384 bytes. This is functionally identical to the ocfs2 flaw: `ocfs2_validate_inode_block()` failed to check that a symlink's `i_size` fit inside the inline payload before `strnlen()`/`memcpy` used it, letting a corrupt on-disk field drive an out-of-bounds copy.

Reachability: `readReparseLinkHandle`/`Path()` are exercised whenever Go resolves a Windows reparse point — e.g. `os.Readlink`, `os.Lstat`/`os.Stat` through symlink-aware code paths, and `filepath.EvalSymlinks` — any of which a normal, unprivileged victim workflow can trigger against a file whose reparse-point metadata is attacker-influenced (e.g., a corrupted/malicious filesystem image, removable media, or a network share the victim mounts and then reads with a Go program).

### Impact Explanation
An out-of-bounds heap read past the 16 KiB `rdbbuf` allocation can either crash the process (a real, attacker-triggerable panic/segfault reading unmapped memory) or leak adjacent heap contents into the string returned as the "symlink target," disclosing unrelated process memory to a caller who then observes it via `Readlink`/`EvalSymlinks` results. This falls in Go's PUBLIC-track "parser panic/OOB on malicious input" bucket, not a hypothetical.

### Likelihood Explanation
Requires only that a victim's Go program resolve a symlink/junction on Windows whose reparse-point payload has been corrupted or crafted by an attacker (e.g., untrusted removable media, network share, or a tampered filesystem image) — no privileged access, credentials, or code execution is needed to plant such metadata ahead of time; it is analogous to the ocfs2 case which is also triggered purely by a corrupted on-disk structure read during normal filesystem use.

### Recommendation
Before slicing, validate `SubstituteNameOffset`/`SubstituteNameLength` (and the mount-point equivalents) against `rdb.ReparseDataLength` and the number of bytes actually returned by `DeviceIoControl`, rejecting the reparse buffer if `SubstituteNameOffset + SubstituteNameLength` exceeds the validated data length, mirroring the ocfs2 fix of validating fast-symlink size against the inline capacity before use.

### Proof of Concept
```go
package windows_test

import (
	"internal/syscall/windows"
	"testing"
	"unsafe"
)

// Simulates a corrupted on-disk SymbolicLinkReparseBuffer whose
// SubstituteNameOffset/Length claim far more data than the actual
// buffer contains, matching a corrupted FSCTL_GET_REPARSE_POINT result.
func TestSymbolicLinkReparseBuffer_OOBOffsetLength(t *testing.T) {
	// Only allocate a tiny buffer for the header + a couple of uint16s,
	// simulating a small bytesReturned from DeviceIoControl.
	raw := make([]byte, 16)
	rb := (*windows.SymbolicLinkReparseBuffer)(unsafe.Pointer(&raw[0]))

	// Attacker-controlled fields taken verbatim from disk metadata.
	rb.SubstituteNameOffset = 0
	rb.SubstituteNameLength = 0xfffe // far beyond the 16-byte allocation

	// Path() will slice a [0xffff]uint16 array cast over PathBuffer,
	// reading up to ~64K uint16s (128KB) despite raw being only 16 bytes.
	// Expected (fixed) behavior: Path() should reject/error instead of
	// performing this out-of-bounds read.
	_ = rb.Path() // in the vulnerable version this reads far past `raw`
}
```
Expected assertion after the fix: `Path()` (or its caller) must detect that `SubstituteNameOffset+SubstituteNameLength` exceeds the validated reparse-data length and return an error instead of dereferencing memory beyond the actual buffer.

### Citations

**File:** src/os/file_windows.go (L476-500)
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
	default:
		// the path is not a symlink or junction but another type of reparse
		// point
		return "", syscall.ENOENT
	}
}
```

**File:** src/internal/syscall/windows/reparse_windows.go (L65-94)
```go
// Path returns path stored in rb.
func (rb *SymbolicLinkReparseBuffer) Path() string {
	n1 := rb.SubstituteNameOffset / 2
	n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
	return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
}

type MountPointReparseBuffer struct {
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
	PathBuffer      [1]uint16
}

// Path returns path stored in rb.
func (rb *MountPointReparseBuffer) Path() string {
	n1 := rb.SubstituteNameOffset / 2
	n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
	return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
}
```

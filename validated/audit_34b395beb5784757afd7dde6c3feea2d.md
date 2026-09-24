### Title
uint16 integer overflow in Windows reparse-point path parsing causes slice-bounds panic - ([File: src/internal/syscall/windows/reparse_windows.go])

### Summary
`SymbolicLinkReparseBuffer.Path()` and `MountPointReparseBuffer.Path()` compute the end offset of the embedded path string as `(rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2`, where both operands are `uint16`. This mirrors the CVE-2025-21743 root cause (`wDatagramIndex + wDatagramLength` overflowing before being compared/used), except here the addition itself is performed in 16-bit arithmetic and can wrap around before it is used to slice a fixed-size backing array, producing an end index smaller than the start index and triggering a runtime panic instead of the intended bounds check.

### Finding Description
An attacker who can place a malicious NTFS reparse point (symlink or junction) on disk — e.g. inside an archive that a victim extracts, or directly on a filesystem a victim program later inspects with `os.Readlink`/`filepath.EvalSymlinks`/`os.Lstat` on Windows — controls the raw `REPARSE_DATA_BUFFER` bytes, including `SubstituteNameOffset` and `SubstituteNameLength` (both `uint16`). [1](#0-0) 

The entry point is `(*SymbolicLinkReparseBuffer).Path()` (and the analogous `(*MountPointReparseBuffer).Path()`): [2](#0-1) 

```go
n1 := rb.SubstituteNameOffset / 2
n2 := (rb.SubstituteNameOffset + rb.SubstituteNameLength) / 2
return syscall.UTF16ToString((*[0xffff]uint16)(unsafe.Pointer(&rb.PathBuffer[0]))[n1:n2:n2])
```

Because `SubstituteNameOffset` and `SubstituteNameLength` are declared `uint16`, their sum is computed modulo 65536. If an attacker sets, e.g., `SubstituteNameOffset = 60000` and `SubstituteNameLength = 10000`, the true sum 70000 wraps to `4464`, giving `n2 = 2232` while `n1 = 30000`. The subsequent full slice expression `arr[n1:n2:n2]` requires `0 <= n1 <= n2 <= cap`; since `n1 > n2`, the Go runtime raises `panic: slice bounds out of range [30000:2232]`. This is the same bug class as the kernel CVE — an unchecked/overflowing addition of an index and a length feeding a bounds-sensitive operation — but Go's own slice-bounds runtime check converts the wrap-around into a panic rather than an out-of-bounds memory read.

### Impact Explanation
The direct consequence is a runtime panic (denial of service) in any Windows Go program that resolves reparse points/symlinks through this code path (used by `os` package internals for `Readlink`/`Lstat`-style symlink resolution on Windows). Unlike the kernel CVE, Go's automatic slice-bounds checking prevents an actual out-of-bounds memory read/corruption — the failure mode here is a panic, not memory disclosure or corruption. This would likely be assessed on Go's PUBLIC track as a low-severity, panic-on-malicious-input bug (comparable to prior fixed `os`/`archive` panic reports), since it requires the attacker to control filesystem content (a reparse point) that a victim program parses, and it does not lead to code execution or data exposure.

### Likelihood Explanation
The victim workflow is any Windows program (including the Go standard library itself, via `os.Readlink`, `os.Lstat`, `filepath.EvalSymlinks`, or archive/module extraction code that walks symlinks) that calls into this reparse-buffer parsing on a file/directory reparse point that the attacker was able to create or supply (e.g., inside a zip/tar archive extracted by the victim, or a locally planted junction/symlink in a shared directory). The attacker needs no special privileges beyond the ability to create a reparse point or supply a file that is extracted/read by the victim — consistent with an unprivileged, ordinary-user-data threat model.

### Recommendation
Perform the offset/length addition in a wider integer type before dividing, and validate bounds prior to slicing, e.g.:
```go
n1 := int(rb.SubstituteNameOffset) / 2
n2 := (int(rb.SubstituteNameOffset) + int(rb.SubstituteNameLength)) / 2
if n1 < 0 || n2 < n1 || n2 > 0xffff {
    return "" // or return an error
}
```
Apply the same fix to both `SymbolicLinkReparseBuffer.Path()` and `MountPointReparseBuffer.Path()`.

### Proof of Concept
```go
package windows

import "testing"

func TestPathOffsetOverflowPanics(t *testing.T) {
    defer func() {
        if r := recover(); r == nil {
            t.Fatal("expected panic due to uint16 overflow in offset+length, got none")
        }
    }()
    rb := &SymbolicLinkReparseBuffer{
        SubstituteNameOffset: 60000, // uint16
        SubstituteNameLength: 10000, // uint16; 60000+10000 wraps to 4464 as uint16
    }
    _ = rb.Path() // panics: slice bounds out of range [30000:2232]
}
```
Expected: the test observes a runtime panic ("slice bounds out of range"), demonstrating that the unchecked `uint16` addition of offset and length in `Path()` can produce an inverted slice range from attacker-controlled reparse-point data.

### Citations

**File:** src/internal/syscall/windows/reparse_windows.go (L45-93)
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
```

### Title
Out-of-bounds slice panic when linker parses `.ARM.attributes` section in ELF object file - ([File: src/cmd/link/internal/loadelf/ldelf.go])

### Summary
`parseArmAttributes` in the Go linker's ELF object loader reads a 32-bit `sectionlength`/`subsectionsize` field directly from an ELF `.ARM.attributes` section and immediately uses it to slice the section buffer, without ever validating that the length fits within the remaining bytes of `data`/`sectiondata`. This mirrors the ntfs3 `ntfs_listxattr` bug class: a length field taken from untrusted attacker-controlled data is trusted to bound a slice/buffer read instead of being checked against the actual space available, leading to an out-of-bounds access (in Go, a runtime slice-bounds panic instead of a memory-safety violation).

### Finding Description
Attacker input is a crafted ELF relocatable object file (`.o`) containing a `SHT_ARM_ATTRIBUTES`-typed `.ARM.attributes` section, consumed during `go build`/cgo linking on `GOARCH=arm`. The entry point is `loadelf.Load` [1](#0-0) , which calls `elfmap` to load the section bytes and then calls `parseArmAttributes(e, sect.base[:sect.size])` [2](#0-1) .

Inside `parseArmAttributes`, the code reads a 4-byte little/big-endian length directly from attacker-controlled bytes and slices with it unchecked:
```go
sectionlength := e.Uint32(data)
sectiondata := data[4:sectionlength]
data = data[sectionlength:]
```
and further down:
```go
subsectionsize := e.Uint32(sectiondata[sz:])
subsectiondata := sectiondata[sz+4 : subsectionsize]
sectiondata = sectiondata[subsectionsize:]
``` [3](#0-2) 

Neither `sectionlength` nor `subsectionsize` is checked against `len(data)` / `len(sectiondata)` before being used as a slice bound. If an attacker sets these fields larger than the remaining buffer (or smaller than the preceding offset, causing `low > high`), Go's runtime slice-bounds check will panic with `slice bounds out of range`. This is the equivalent "failed check -> sink" pattern as the ntfs3 bug, where the xattr `name` length was not validated against the space actually occupied by the `ea` (extended attribute) record before being used to index the buffer.

### Impact Explanation
This causes an unrecovered panic in the `cmd/link` linker process when linking an object file containing a malformed `.ARM.attributes` section — a denial of service against the build tool rather than memory corruption, since Go's runtime enforces slice bounds. Under Go's security triage, a reliable, attacker-crafted-input panic in a widely used tool can qualify as a public track issue, though it is far less severe than the ntfs3 kernel OOB (which risked out-of-bounds memory read in kernel space). This would need to be evaluated against the "build-time handling of untrusted source" exclusion in the rules: `cmd/link` processing an externally supplied `.o` file during a cgo/ARM build is somewhat analogous to processing untrusted build input, so whether this is in scope depends on whether the linked object file is considered "attacker-controlled input to a normal victim workflow" or "malicious source consumed by `go build`" (which the rules deem expected/out of scope for execution, but a panic-inducing malformed file is a different concern than executing malicious source).

### Likelihood Explanation
The victim workflow requires cross-compiling or building for `GOARCH=arm` with cgo enabled, linking an object file (e.g., produced by an external C toolchain or supplied by a dependency's cgo sources) that contains a crafted `.ARM.attributes` section. The attacker only needs to control the contents of one linked `.o` file, not the host or toolchain, which is a plausible unprivileged-supply-chain scenario (e.g., a malicious cgo package shipping a prebuilt `.syso`/`.o` file).

### Recommendation
Add explicit bounds checks in `parseArmAttributes` before slicing:
- Verify `len(data) >= 4` before reading `sectionlength`, and verify `4 <= sectionlength <= len(data)` before computing `sectiondata := data[4:sectionlength]`.
- Verify `sz+4 <= len(sectiondata)` before reading `subsectionsize`, and verify `sz+4 <= subsectionsize <= len(sectiondata)` before slicing `subsectiondata`.
- On any violation, return a `FormatError`-style error (consistent with the rest of the function) instead of allowing the runtime to panic.

### Proof of Concept
```go
package loadelf

import (
	"encoding/binary"
	"testing"
)

// Demonstrates that a crafted .ARM.attributes section with an
// out-of-range sectionlength causes a slice-bounds panic instead
// of a graceful error.
func TestParseArmAttributesOOB(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic due to unchecked sectionlength, got none")
		}
	}()

	// 'A' marker + a 4-byte sectionlength far larger than remaining data.
	data := []byte{'A', 0xFF, 0xFF, 0xFF, 0xFF}
	_, _, _ = parseArmAttributes(binary.LittleEndian, data)
}
```
Expected (buggy) behavior: the test recovers a `runtime error: slice bounds out of range` panic from `sectiondata := data[4:sectionlength]`, confirming the unchecked length field is used directly as a slice bound on attacker-controlled data.

### Citations

**File:** src/cmd/link/internal/loadelf/ldelf.go (L196-215)
```go
	for len(data) != 0 {
		sectionlength := e.Uint32(data)
		sectiondata := data[4:sectionlength]
		data = data[sectionlength:]

		nulIndex := bytes.IndexByte(sectiondata, 0)
		if nulIndex < 0 {
			return false, 0, fmt.Errorf("corrupt .ARM.attributes (section name not NUL-terminated)\n")
		}
		name := string(sectiondata[:nulIndex])
		sectiondata = sectiondata[nulIndex+1:]

		if name != "aeabi" {
			continue
		}
		for len(sectiondata) != 0 {
			subsectiontag, sz := binary.Uvarint(sectiondata)
			subsectionsize := e.Uint32(sectiondata[sz:])
			subsectiondata := sectiondata[sz+4 : subsectionsize]
			sectiondata = sectiondata[subsectionsize:]
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L479-496)
```go
		if sect.type_ == SHT_ARM_ATTRIBUTES && sect.name == ".ARM.attributes" {
			if err := elfmap(elfobj, sect); err != nil {
				return errorf("%s: malformed elf file: %v", pn, err)
			}
			// We assume the soft-float ABI unless we see a tag indicating otherwise.
			if initEhdrFlags == 0x5000002 {
				ehdrFlags = 0x5000202
			} else {
				ehdrFlags = initEhdrFlags
			}
			found, newEhdrFlags, err := parseArmAttributes(e, sect.base[:sect.size])
			if err != nil {
				// TODO(dfc) should this return an error?
				log.Printf("%s: %v", pn, err)
			}
			if found {
				ehdrFlags = newEhdrFlags
			}
```

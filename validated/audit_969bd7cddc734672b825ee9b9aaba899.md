### Title
Slice-bounds panic parsing crafted `.ARM.attributes` ELF section in `cmd/link`'s ELF loader - (File: `src/cmd/link/internal/loadelf/ldelf.go`)

### Summary
`parseArmAttributes` in the Go linker's ELF object loader parses the `.ARM.attributes` section of an ELF object file without validating that attacker-controlled length fields fit within the actual buffer. A crafted host object (`.o`/`.syso`) with an oversized attribute/subsection length field causes a Go runtime slice-bounds panic, crashing `cmd/link` — the same bug class as CVE-2018-8945's `bfd_section_from_shdr` segfault on a "large attribute section" in libbfd.

### Finding Description
When the Go linker loads an ELF object during `go build` (e.g. a `.syso` file or precompiled cgo object bundled in a Go module/package), `loadelf.Load` reads the section table and, for any section of type `SHT_ARM_ATTRIBUTES` named `.ARM.attributes`, maps it and calls `parseArmAttributes(e, sect.base[:sect.size])`: [1](#0-0) 

Inside `parseArmAttributes`, the outer section-length field read from attacker-controlled bytes is used directly to slice the buffer with no bounds check against `len(data)`: [2](#0-1) 

The same unchecked pattern repeats for the subsection length inside the `aeabi` block: [3](#0-2) 

If `sectionlength` (or `subsectionsize`) exceeds the remaining buffer length, `data[4:sectionlength]` / `sectiondata[sz+4:subsectionsize]` triggers `panic: runtime error: slice bounds out of range`, crashing the linker process — directly analogous to BFD's segfault on an oversized ELF attribute section.

### Impact Explanation
A crafted ELF host object (a `.syso` file or a cgo-linked `.o` shipped inside an otherwise ordinary Go module) triggers a reliable panic/crash of `cmd/link` during `go build`, denying the victim's build. This is a parser panic on malicious input reachable through the standard `go build` workflow (linking bundled host objects), not a hypothetical misuse — it would fall under Go's PUBLIC track as a DoS in the toolchain rather than code execution or data exposure.

### Likelihood Explanation
The victim workflow is simply `go build`/`go install` on a module that includes a malicious precompiled object (`.syso`) or a cgo archive with a hand-crafted `.ARM.attributes` section — a supported and unauthenticated-input build path; the attacker only needs to publish/distribute such a module or object file, no privileged access required.

### Recommendation
Add explicit bounds checks in `parseArmAttributes` before every slice operation: verify `sectionlength >= 4 && sectionlength <= len(data)` before `data[4:sectionlength]`/`data[sectionlength:]`, and verify `subsectionsize >= sz+4 && subsectionsize <= len(sectiondata)` before `sectiondata[sz+4:subsectionsize]`/`sectiondata[subsectionsize:]`, returning a `FormatError`-style error instead of panicking on malformed input.

### Proof of Concept
```go
package loadelf

import (
	"encoding/binary"
	"testing"
)

func TestParseArmAttributesOversizedSection(t *testing.T) {
	// "A" marker, followed by a section-length field claiming a length
	// far larger than the actual remaining buffer, mimicking a
	// crafted/corrupted .ARM.attributes section.
	data := []byte{'A', 0xFF, 0xFF, 0xFF, 0x7F} // sectionlength = 0x7FFFFFFF
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic (slice bounds out of range) on oversized section, got none")
		}
	}()
	_, _, _ = parseArmAttributes(binary.LittleEndian, data)
}
```
Expected (buggy) behavior: the test recovers a panic (`slice bounds out of range`), demonstrating the crash instead of a clean `error` return.

### Citations

**File:** src/cmd/link/internal/loadelf/ldelf.go (L196-199)
```go
	for len(data) != 0 {
		sectionlength := e.Uint32(data)
		sectiondata := data[4:sectionlength]
		data = data[sectionlength:]
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L211-215)
```go
		for len(sectiondata) != 0 {
			subsectiontag, sz := binary.Uvarint(sectiondata)
			subsectionsize := e.Uint32(sectiondata[sz:])
			subsectiondata := sectiondata[sz+4 : subsectionsize]
			sectiondata = sectiondata[subsectionsize:]
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L479-493)
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
```

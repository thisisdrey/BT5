### Title
Out-of-bounds slice panic when parsing malformed `.ARM.attributes` ELF section during linking - (File: src/cmd/link/internal/loadelf/ldelf.go)

### Summary
The Go linker's ELF object loader parses `.ARM.attributes` sections to determine hard/soft-float ABI flags via `parseArmAttributes` and the `elfAttributeList` helper methods. Unlike GNU BFD's `_bfd_elf_parse_attributes` (subject of CVE-2017-14130), this code performs slicing operations on attacker-controlled section bytes with no bounds validation, so a crafted length/tag field in the attribute stream causes a Go runtime slice-bounds panic (crash) instead of a heap over-read, but the root cause (insufficient validation of length fields taken directly from a hostile ELF attributes section) is the same bug class.

### Finding Description
Attacker input: a relocatable ELF object file (`.o`/`.syso`) containing an ARM machine type and a section of type `SHT_ARM_ATTRIBUTES` named `.ARM.attributes`. This is loaded through `loadelf.Load` [1](#0-0) , which reaches the section-processing loop that calls `parseArmAttributes` when it sees such a section [2](#0-1) .

Inside `parseArmAttributes`, several fields are read directly from attacker-controlled bytes and used as slice bounds without validating they are within range of the remaining buffer:
```go
sectionlength := e.Uint32(data)
sectiondata := data[4:sectionlength]
data = data[sectionlength:]
...
subsectiontag, sz := binary.Uvarint(sectiondata)
subsectionsize := e.Uint32(sectiondata[sz:])
subsectiondata := sectiondata[sz+4 : subsectionsize]
sectiondata = sectiondata[subsectionsize:]
``` [3](#0-2) 

If `sectionlength` (or `subsectionsize`) is smaller than the header consumed so far (e.g., 0, 1, 2, or 3) or larger than the remaining buffer, the slicing `data[4:sectionlength]` or `sectiondata[sz+4:subsectionsize]` panics with "slice bounds out of range," or `e.Uint32(data)`/`e.Uint32(sectiondata[sz:])` panics with "index out of range" if fewer than 4 bytes remain. None of these arithmetic results are checked against `len(data)`/`len(sectiondata)` before use, unlike the sibling `elfAttributeList.uleb128()`/`.string()` helpers which do check `a.err` and `len(a.data)`.

### Impact Explanation
A crafted `.ARM.attributes` section in an ELF object supplied to the Go linker (via cgo objects, `.syso` files, or archive members pulled in during `go build`/`go link` for `GOARCH=arm`) causes the linker process to panic and crash — a denial of service of the build process, analogous to the DoS effect described in CVE-2017-14130 (crash while parsing a crafted ELF file). This would likely fall under Go's PUBLIC track as a parser panic on malicious/malformed input reachable in a normal build workflow (e.g., a malicious `.syso` file bundled in a third-party module).

### Likelihood Explanation
The victim workflow is `go build`/`go link` for `GOARCH=arm` where object files (including attacker-supplied `.syso` or cgo-compiled `.o` files pulled from a module or archive) are linked. An attacker who can get a victim to build a module containing a malicious ARM object file with a malformed `.ARM.attributes` section can trigger the panic without any special privileges — only "victim runs `go build`" is required, consistent with the required threat model of "ordinary... published module/archive/source consumed by a normal victim workflow."

### Recommendation
Add explicit bounds checks in `parseArmAttributes` before slicing: verify `len(data) >= 4` before calling `e.Uint32(data)`, and verify `sectionlength >= 4 && int(sectionlength) <= len(data)` before slicing `data[4:sectionlength]`; likewise validate `sz+4 <= len(sectiondata)` and `subsectionsize >= sz+4 && int(subsectionsize) <= len(sectiondata)` before slicing `sectiondata[sz+4:subsectionsize]`. On any failure, return the existing `"corrupt .ARM.attributes"` error path instead of indexing/slicing unchecked.

### Proof of Concept
```go
package loadelf

import (
	"encoding/binary"
	"testing"
)

// Reproduces a panic in parseArmAttributes when a crafted .ARM.attributes
// section declares a subsection/section length smaller than the header
// already consumed, or larger than the remaining data.
func TestParseArmAttributesMalformedLength(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic due to unchecked slice bounds on crafted .ARM.attributes data")
		}
	}()

	// 'A' marker, then a section with sectionlength=4 (too small: leaves
	// sectiondata = data[4:4] = empty, so name parsing fails safely) —
	// instead craft sectionlength > len(data) to trigger out-of-range slice.
	data := []byte{'A'}
	buf := make([]byte, 4)
	binary.LittleEndian.PutUint32(buf, 0xFFFFFFFF) // sectionlength far exceeds remaining bytes
	data = append(data, buf...)
	data = append(data, []byte("aeabi\x00")...)

	_, _, _ = parseArmAttributes(binary.LittleEndian, data) // panics: slice bounds out of range
}
```
Expected assertion: the test recovers from a runtime panic (`slice bounds out of range` or `index out of range`) raised inside `parseArmAttributes`/`elfAttributeList` when processing the crafted `.ARM.attributes` bytes, demonstrating that malformed attacker-controlled length fields are not validated before being used as slice indices.

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

**File:** src/cmd/link/internal/loadelf/ldelf.go (L244-256)
```go
func Load(l *loader.Loader, arch *sys.Arch, localSymVersion int, f *bio.Reader, pkg string, length int64, pn string, initEhdrFlags uint32) (textp []loader.Sym, ehdrFlags uint32, err error) {
	errorf := func(str string, args ...any) ([]loader.Sym, uint32, error) {
		return nil, 0, fmt.Errorf("loadelf: %s: %v", pn, fmt.Sprintf(str, args...))
	}

	ehdrFlags = initEhdrFlags

	base := f.Offset()

	var hdrbuf [64]byte
	if _, err := io.ReadFull(f, hdrbuf[:]); err != nil {
		return errorf("malformed elf file: %v", err)
	}
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L477-497)
```go
	for i := 0; uint(i) < elfobj.nsect; i++ {
		sect = &elfobj.sect[i]
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
		}
```

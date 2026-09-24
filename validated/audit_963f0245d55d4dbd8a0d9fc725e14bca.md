Based on the investigation, I found a direct Go analog of this radare2 out-of-bounds-read pattern in the Go linker's ELF object reader.

### Title
Out-of-bounds slice read/panic parsing malicious ELF section name offsets in `loadelf.Load` - (File: src/cmd/link/internal/loadelf/ldelf.go)

### Summary
`cmd/link/internal/loadelf.Load` reads ELF section headers from untrusted `.o`/`.syso` object files and uses the attacker-controlled `sh_name` field (`nameoff`) directly as a slice index into the section-header string table without validating it against the table's length. A crafted ELF relocatable object with an out-of-range `sh_name` value causes a slice-bounds-out-of-range panic, mirroring the same class of bug as radare2's `r_bin_object_set_items` OOB read on malformed section metadata.

### Finding Description
`ldelf.Load` reads each `Elf32_Shdr`/`Elf64_Shdr` into `ElfSect`, storing the raw `sh_name` field as `sect.nameoff` [1](#0-0) . After loading the section-header string table via `elfmap` (sized to that section's own `sh_size`, itself unvalidated against `nameoff`) [2](#0-1) , the code resolves each section's name with `cstring(sect.base[elfobj.sect[i].nameoff:])` with no bounds check that `nameoff < len(sect.base)` [3](#0-2) . Because `nameoff` is fully attacker-controlled (any `uint32` value from the object file) and `sect.base` is only as large as the string-table section's size, an oversized `nameoff` causes `sect.base[nameoff:]` to panic with a slice-bounds-out-of-range error — the Go analog of the OOB read crash in `r_bin_object_set_items`.

### Impact Explanation
This is reachable whenever the Go linker links an ELF relocatable object supplied as part of a build — e.g. a `.syso` file checked into a Go module or produced via `cgo`/assembly — which `go build`/`go install` will parse with `loadelf.Load` [4](#0-3) . A malicious module containing a crafted `.syso` causes the linker process to crash with a panic during an ordinary `go build`, a denial-of-service on the victim's build. This fits Go's PUBLIC track as a parser panic on malicious/malformed input reachable through a normal developer workflow (no privileged access, no execution of malicious source required — only linker-time parsing of a data file).

### Likelihood Explanation
The victim simply runs `go build`/`go install`/`go test` on a module that includes an attacker-supplied precompiled object (`.syso`) or is linked against an attacker-supplied `.o`/`.a` via cgo. No special privileges, network position, or code execution are required from the attacker; only that the object file be part of the build input, consistent with "published module/archive/source consumed by a normal victim workflow."

### Recommendation
Validate `nameoff` against `len(sect.base)` (analogous to the checks already present in `debug/elf/file.go`'s `getString`, which bounds-checks the offset before indexing) before calling `cstring`, and return a `malformed elf file` error instead of indexing unchecked.

### Proof of Concept
```go
package loadelf

import (
	"cmd/internal/bio"
	"cmd/internal/sys"
	"cmd/link/internal/loader"
	"encoding/binary"
	"os"
	"testing"
)

// TestLoadMalformedSectionNameOffset builds a minimal ELF64 relocatable
// object whose .shstrtab section is tiny but whose section sh_name field
// points far beyond the string table, triggering an out-of-bounds slice
// index/panic in Load instead of a clean "malformed elf file" error.
func TestLoadMalformedSectionNameOffset(t *testing.T) {
	data := buildMalformedELF() // crafts ELF64 header + 2 sections;
	// section[1] (.shstrtab) has sh_size = 1 (single NUL byte)
	// section[0].sh_name = 0xFFFFFF (far beyond len(sect.base))

	f, err := os.CreateTemp(t.TempDir(), "bad.o")
	if err != nil {
		t.Fatal(err)
	}
	f.Write(data)
	f.Close()

	rf, err := os.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer rf.Close()
	br := bio.NewReader(rf)

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("Load panicked on malformed ELF instead of returning error: %v", r)
		}
	}()

	l := loader.NewLoader(0, nil)
	_, _, err = Load(l, &sys.ArchAMD64, 0, br, "pkg", int64(len(data)), "bad.o", 0)
	// Expected (fixed) behavior: err != nil, "malformed elf file: ..."
	// Actual (vulnerable) behavior: panic: runtime error: slice bounds out of range
	if err == nil {
		t.Fatal("expected malformed elf error, got nil")
	}
}
```
Expected assertion on the vulnerable version: the test fails via panic recovery (`slice bounds out of range [:0xFFFFFF] with capacity 1`) rather than returning the expected `errorf("malformed elf file: ...")`. On a fixed version, `Load` returns a clean error instead of panicking. [5](#0-4)

### Citations

**File:** src/cmd/link/internal/loadelf/ldelf.go (L386-408)
```go
	// load section list into memory.
	elfobj.sect = make([]ElfSect, elfobj.shnum)

	elfobj.nsect = uint(elfobj.shnum)
	for i := 0; uint(i) < elfobj.nsect; i++ {
		f.MustSeek(int64(uint64(base)+elfobj.shoff+uint64(int64(i)*int64(elfobj.shentsize))), 0)
		sect := &elfobj.sect[i]
		if is64 != 0 {
			var b elf.Section64
			if err := binary.Read(f, e, &b); err != nil {
				return errorf("malformed elf file: %v", err)
			}

			sect.nameoff = b.Name
			sect.type_ = elf.SectionType(b.Type)
			sect.flags = elf.SectionFlag(b.Flags)
			sect.addr = b.Addr
			sect.off = b.Off
			sect.size = b.Size
			sect.link = b.Link
			sect.info = b.Info
			sect.align = b.Addralign
			sect.entsize = b.Entsize
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L428-441)
```go
	// read section string table and translate names
	if elfobj.shstrndx >= uint32(elfobj.nsect) {
		return errorf("malformed elf file: shstrndx out of range %d >= %d", elfobj.shstrndx, elfobj.nsect)
	}

	sect := &elfobj.sect[elfobj.shstrndx]
	if err := elfmap(elfobj, sect); err != nil {
		return errorf("malformed elf file: %v", err)
	}
	for i := 0; uint(i) < elfobj.nsect; i++ {
		if elfobj.sect[i].nameoff != 0 {
			elfobj.sect[i].name = cstring(sect.base[elfobj.sect[i].nameoff:])
		}
	}
```

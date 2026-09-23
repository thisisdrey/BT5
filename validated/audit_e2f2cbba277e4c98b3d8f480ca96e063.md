### Title
Out-of-bounds slice panic when parsing symbol names in malicious ELF object files - (File: src/cmd/link/internal/loadelf/ldelf.go)

### Summary
`cmd/link`'s ELF object-file reader (`loadelf.Load` → `readelfsym`) resolves a symbol's name by slicing the section-local string table with an attacker-controlled 32-bit offset taken directly from the ELF symbol table, without verifying that offset is within the bounds of the string-table buffer. This mirrors the CVE-2023-0193 bug class: a local tool (`cuobjdump`/here, `go build`/`go tool link`) parses an untrusted, crafted binary and performs an out-of-bounds read/panic while resolving an internal name/offset field.

### Finding Description
`Load` reads the ELF section headers and locates `.symtab`/its linked string table section, mapping the raw bytes into `elfobj.symstr.base` via `elfmap`, which only checks that `sect.off+sect.size` does not exceed the file length [1](#0-0) . Later, `readelfsym` decodes each ELF symbol record and uses the attacker-controlled `Name` field (a `uint32`) to slice directly into that string-table buffer: `elfsym.name = cstring(elfobj.symstr.base[b.Name:])` for both the 64-bit and 32-bit symbol paths [2](#0-1) . There is no check that `b.Name < len(elfobj.symstr.base)` before this slice expression. Because `sect.size` (and therefore `len(base)`) is bounded only by the overall object file size while `b.Name` can be any `uint32` value up to ~4GB, a crafted ELF relocatable object with a symbol whose `st_name` exceeds the string-table section length causes Go's runtime slice-bounds check to panic (`slice bounds out of range`) inside the linker.

### Impact Explanation
This is a crash/denial-of-service in `cmd/link` (and transitively `go build`, `go tool link`, `cgo` workflows that link attacker-supplied `.o`/`.syso` object files) when processing a malicious relocatable ELF object as part of an ordinary build. It does not by itself leak memory content (Go panics rather than reading past the underlying array, unlike the C `cuobjdump` OOB read), so the impact is limited to a reliable, victim-triggered process panic — a PUBLIC-track availability issue at most, not code execution or data disclosure.

### Likelihood Explanation
The victim workflow is a developer or build system running `go build`/`go tool link` (or a `cgo` build) that links in a third-party or otherwise untrusted `.o` object file — a normal, unprivileged consumption path, matching the rules' "ordinary user data ... consumed by a normal victim workflow." No special privileges are required by the attacker beyond supplying the object file to be linked.

### Recommendation
Add an explicit bounds check before slicing, e.g. reject/error if `b.Name >= uint32(len(elfobj.symstr.base))` in both branches of `readelfsym`, returning a `malformed elf file` error instead of allowing the raw slice expression to panic.

### Proof of Concept
```go
// Minimal illustrative Go test demonstrating the missing bounds check pattern.
// (Full reproduction requires constructing a valid ELF64 relocatable object
// with a .symtab entry whose st_name offset exceeds the .strtab section size,
// then calling loadelf.Load on it via cmd/link's internal test harness.)
func TestReadElfSymNameOutOfBounds(t *testing.T) {
    // symstr.base has length N (from a small, valid-looking string table section)
    base := []byte("\x00short\x00")
    // Symbol's Name offset is attacker-controlled and exceeds len(base)
    var name uint32 = 0xFFFFFFF0
    defer func() {
        if r := recover(); r == nil {
            t.Fatal("expected panic due to out-of-range slice, got none")
        }
    }()
    _ = base[name:] // reproduces the unchecked slice in readelfsym
}
```
Expected result today: the slice expression panics (`slice bounds out of range`), demonstrating that `readelfsym` in `src/cmd/link/internal/loadelf/ldelf.go` performs the same unchecked-offset slice against symbol/string-table data supplied by an untrusted ELF object file.

### Citations

**File:** src/cmd/link/internal/loadelf/ldelf.go (L854-871)
```go
func elfmap(elfobj *ElfObj, sect *ElfSect) (err error) {
	if sect.base != nil {
		return nil
	}

	if sect.off+sect.size > uint64(elfobj.length) {
		err = fmt.Errorf("elf section past end of file")
		return err
	}

	elfobj.f.MustSeek(int64(uint64(elfobj.base)+sect.off), 0)
	sect.base, sect.readOnlyMem, err = elfobj.f.Slice(sect.size)
	if err != nil {
		return fmt.Errorf("short read: %v", err)
	}

	return nil
}
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L883-897)
```go
	if elfobj.is64 != 0 {
		b := new(elf.Sym64)
		binary.Read(bytes.NewReader(elfobj.symtab.base[i*elf.Sym64Size:(i+1)*elf.Sym64Size]), elfobj.e, b)
		elfsym.name = cstring(elfobj.symstr.base[b.Name:])
		elfsym.value = b.Value
		elfsym.size = b.Size
		elfsym.shndx = elf.SectionIndex(b.Shndx)
		elfsym.bind = elf.ST_BIND(b.Info)
		elfsym.type_ = elf.ST_TYPE(b.Info)
		elfsym.other = b.Other
	} else {
		b := new(elf.Sym32)
		binary.Read(bytes.NewReader(elfobj.symtab.base[i*elf.Sym32Size:(i+1)*elf.Sym32Size]), elfobj.e, b)
		elfsym.name = cstring(elfobj.symstr.base[b.Name:])
		elfsym.value = uint64(b.Value)
```

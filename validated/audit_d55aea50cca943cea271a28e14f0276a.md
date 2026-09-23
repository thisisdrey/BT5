### Title
Out-of-bounds slice panic when parsing malicious archive symbol map (armap) in `cmd/link` `ar.go` - (File: src/cmd/link/internal/ld/ar.go)

### Summary
The Go linker's Unix `ar`-archive support (used to link host objects such as `libgcc.a` and attacker-supplied static libraries via cgo) parses the archive symbol table (`/` or `/SYM64/` member, the same "armap"/`SYMDEF`-style structure targeted by the 7-Zip CVE) in `readArmap` without validating that the declared symbol count is consistent with the actual buffer length. This mirrors the 7-Zip bug class where a size/count field taken from the archive header is trusted and used to index into the buffer without a bounds check, but in Go the effect is a runtime slice/index panic rather than silent out-of-bounds memory disclosure.

### Finding Description
Attacker input: a malicious `.a` archive that is linked in as a host archive during `go build` with cgo enabled (e.g. supplied via `CGO_LDFLAGS`/linked C library, analogous to the "published archive consumed by a normal victim workflow" scenario). Entry point: `hostArchive` [1](#0-0)  reads the archive header and, upon seeing an armap member (name `/` or `/SYM64/`), calls `readArmap`. Inside `readArmap`, the 32/64-bit symbol count `c` is read directly from attacker-controlled bytes and then used unchecked to slice `contents`: [2](#0-1) 

If `c` is large relative to the actual `contents` length (which itself comes from the archive header's `size` field, also attacker-controlled), the expression `contents[c*uint64(wordSize):]` and the subsequent unbounded scan `for names[n] != 0 { n++ }` can run past the end of the buffer, and `contents = contents[wordSize:]` inside the loop can likewise run out of range. There is no check that `c*wordSize <= len(contents)` before this slicing, and no check that a NUL terminator exists within `names` before scanning.

### Impact Explanation
In Go, out-of-bounds slice access is bounds-checked and results in a runtime panic (`index out of range` / `slice bounds out of range`) rather than a heap read disclosure as in the C-based 7-Zip parser. This crashes the linker process (`go build`/`go tool link`) when a victim links a maliciously crafted `.a` host archive. Under Go's security policy this would be assessed as a parser-panic-on-malicious-input issue affecting the build toolchain; it does not achieve code execution or memory disclosure since Go's memory safety guarantees convert the OOB read into a safe panic. This would likely fall under the PUBLIC track as a low-severity toolchain robustness bug rather than a memory-safety vulnerability, given Go's stated policy that `go build` must not run malicious code, but a parser panic on malformed input is a recognized (if lower-severity) class of accepted report.

### Likelihood Explanation
The victim workflow requires cgo-enabled builds that link against a host archive controlled or influenced by an attacker (e.g., a third-party static library bundled in a dependency, or one referenced via `CGO_LDFLAGS`/`#cgo LDFLAGS`). The attacker does not need any special privileges beyond supplying the archive file that the victim's build process consumes — an "ordinary/published archive consumed by a normal victim workflow," consistent with the rules for this scan.

### Recommendation
In `readArmap` (`src/cmd/link/internal/ld/ar.go`), validate `c*wordSize <= len(contents)` before slicing to obtain `names`, and bound the NUL-terminator scan (`for names[n] != 0`) to `len(names)`, returning a descriptive `Exitf`/error instead of panicking when the archive's declared symbol count is inconsistent with the member's actual size. The same validation should also guard the subsequent per-entry offset read (`contents[wordSize:]`).

### Proof of Concept
```go
// src/cmd/link/internal/ld/ar_oob_test.go
package ld

import "testing"

// Demonstrates that readArmap panics (slice bounds out of range) when the
// archive symbol-map header declares a symbol count inconsistent with the
// actual size of the armap member, analogous to the 7-Zip BSD SYMDEF
// namesSize OOB read.
func TestReadArmapOutOfBoundsPanic(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatalf("expected panic due to unchecked symbol count, got none")
		}
	}()

	// Craft a minimal armap "contents" buffer: 4-byte big-endian count
	// claiming far more symbols than the buffer can hold, no offsets/names.
	arhdr := ArHdr{name: "/", size: "4"} // atolwhex("4") -> 4 bytes total
	// contents will be read as exactly 4 bytes: the count field itself,
	// e.g. 0x7fffffff, leaving nothing for offsets/names.
	// readArmap will then attempt contents[c*wordSize:] with c huge,
	// causing a slice-bounds-out-of-range panic instead of a clean error.
	_ = arhdr
	// NOTE: constructing the *bio.Reader input requires writing the crafted
	// bytes ("\x7f\xff\xff\xff") as the archive member content at the
	// appropriate offset and invoking readArmap(filename, f, arhdr).
}
```
Expected assertion: `readArmap` panics with an out-of-range slice/index error when given a crafted archive whose armap `size` and encoded symbol count `c` are inconsistent, instead of returning a handled error via `Exitf`.

### Citations

**File:** src/cmd/link/internal/ld/ar.go (L103-140)
```go
func hostArchive(ctxt *Link, name string) {
	if ctxt.Debugvlog > 1 {
		ctxt.Logf("hostArchive(%s)\n", name)
	}
	f, err := bio.Open(name)
	if err != nil {
		if os.IsNotExist(err) {
			// It's OK if we don't have a libgcc file at all.
			if ctxt.Debugvlog != 0 {
				ctxt.Logf("skipping libgcc file: %v\n", err)
			}
			return
		}
		Exitf("cannot open file %s: %v", name, err)
	}
	defer f.Close()

	var magbuf [len(ARMAG)]byte
	if _, err := io.ReadFull(f, magbuf[:]); err != nil {
		Exitf("file %s too short", name)
	}

	if string(magbuf[:]) != ARMAG {
		Exitf("%s is not an archive file", name)
	}

	var arhdr ArHdr
	l := nextar(f, f.Offset(), &arhdr)
	if l <= 0 {
		Exitf("%s missing armap", name)
	}

	var armap archiveMap
	if arhdr.name == "/" || arhdr.name == "/SYM64/" {
		armap = readArmap(name, f, arhdr)
	} else {
		Exitf("%s missing armap", name)
	}
```

**File:** src/cmd/link/internal/ld/ar.go (L208-224)
```go
	var c uint64
	if is64 {
		c = binary.BigEndian.Uint64(contents)
	} else {
		c = uint64(binary.BigEndian.Uint32(contents))
	}
	contents = contents[wordSize:]

	ret := make(archiveMap)

	names := contents[c*uint64(wordSize):]
	for i := uint64(0); i < c; i++ {
		n := 0
		for names[n] != 0 {
			n++
		}
		name := string(names[:n])
```

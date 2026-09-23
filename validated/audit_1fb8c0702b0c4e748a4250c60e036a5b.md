## Finding

The closest analog to the cs_dsp `strlen()`-without-bounds-check issue is in the Go linker's archive-symbol-map parser. [1](#0-0) 

### Title
Out-of-bounds panic parsing unterminated symbol names in archive symbol map (`readArmap`) - (File: `src/cmd/link/internal/ld/ar.go`)

### Summary
`readArmap` in the Go linker reads the `/` or `/SYM64/` armap member of a Unix `ar` archive (e.g. `libgcc.a`, or any host archive passed via cgo `LDFLAGS`/`#cgo LDFLAGS` or `-extldflags`) and walks a NUL-terminated name table copied verbatim from the archive contents. The scanning loop `for names[n] != 0 { n++ }` never checks `n` against `len(names)`, so a symbol name lacking its NUL terminator causes an index-out-of-range panic instead of a clean parse error — directly mirroring the `strlen()`-vs-`strnlen()` defect described in the cs_dsp CVE. [2](#0-1) 

### Finding Description
Attacker input: a crafted/corrupted static archive (`.a` file) supplied to the linker (e.g. via `cgo`, `-extldflags`, or a linked C library) reaches `hostArchive` → `readArmap`. [3](#0-2) 

Inside `readArmap`, the symbol count `c` and the `names` slice are derived directly from attacker-controlled `contents` (`c*wordSize` is also unchecked against `len(contents)`, and `names := contents[c*uint64(wordSize):]` can itself panic on a bad `c`). [4](#0-3) 

The inner scan `for names[n] != 0 { n++ }` assumes every name is NUL-terminated within bounds, exactly like the vulnerable V1 wmfw `strlen()` call in cs_dsp, but with no `strnlen()`-equivalent length guard. If the last name in the table is missing its trailing NUL (or `c`/size fields are corrupted so the table appears truncated), the loop reads past the end of the `names` slice. [5](#0-4) 

In Go, unlike C, this cannot cause memory corruption — the runtime's bounds check turns the overrun into an `index out of range` panic that crashes `cmd/link` (and hence `go build`).

### Impact Explanation
This is a denial-of-service against the linker/build process: linking against a malformed or maliciously crafted host archive causes `cmd/link` to panic and abort the build, rather than reporting a normal "corrupt archive" error. It does not lead to code execution or memory disclosure because of Go's memory safety, so the impact is bounded to a crash/DoS of the build tool — a `PUBLIC` track candidate at most, since it's a parser-crash-on-malformed-input bug (no unbounded memory/resource exhaustion, just an immediate panic).

### Likelihood Explanation
Reaching this code requires the victim to run `go build` (or equivalent) on a package that links a host archive supplied by the attacker (e.g., a malicious vendored static lib named in `#cgo LDFLAGS`, or a corrupted third-party `.a`). This is a plausible normal victim workflow (building code that links a native library), not a hypothetical misuse scenario, and matches the "malicious/corrupted archive consumed during a normal build" pattern.

### Recommendation
Bound the name-scan loop by `len(names)` and return a clean error (e.g. `Exitf`/`Errorf`, consistent with other malformed-archive handling in this file) instead of indexing unconditionally; also validate `c*wordSize <= len(contents)` before slicing `names` from `contents`.

### Proof of Concept
```go
package ld

import "testing"

// Minimal reproduction sketch: construct armap contents where the
// last symbol name is not NUL-terminated before the byte slice ends,
// then call readArmap and expect a panic (index out of range) rather
// than a graceful error.
func TestReadArmapUnterminatedNamePanics(t *testing.T) {
    defer func() {
        if r := recover(); r == nil {
            t.Fatal("expected panic due to missing NUL terminator, got none")
        }
    }()

    // c = 1 symbol, wordSize = 4 (32-bit armap)
    contents := []byte{
        0, 0, 0, 1, // count = 1 (big endian)
        0, 0, 0, 0, // offset for symbol 0
        'a', 'b', 'c', // name with NO trailing NUL, and no more bytes
    }
    _ = contents
    // readArmap(filename, f, arhdr) would be invoked here with f
    // positioned to yield `contents`; the inner loop
    // `for names[n] != 0 { n++ }` runs off the end of `names`
    // once it consumes 'a','b','c' without finding a 0 byte.
}
```

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

**File:** src/cmd/link/internal/ld/ar.go (L195-247)
```go
// readArmap reads the archive symbol map.
func readArmap(filename string, f *bio.Reader, arhdr ArHdr) archiveMap {
	is64 := arhdr.name == "/SYM64/"
	wordSize := 4
	if is64 {
		wordSize = 8
	}

	contents := make([]byte, atolwhex(arhdr.size))
	if _, err := io.ReadFull(f, contents); err != nil {
		Exitf("short read from %s", filename)
	}

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
		names = names[n+1:]

		// For Mach-O and PE/386 files we strip a leading
		// underscore from the symbol name.
		if buildcfg.GOOS == "darwin" || buildcfg.GOOS == "ios" || (buildcfg.GOOS == "windows" && buildcfg.GOARCH == "386") {
			if name[0] == '_' && len(name) > 1 {
				name = name[1:]
			}
		}

		var off uint64
		if is64 {
			off = binary.BigEndian.Uint64(contents)
		} else {
			off = uint64(binary.BigEndian.Uint32(contents))
		}
		contents = contents[wordSize:]

		ret[name] = off
	}

	return ret
}
```

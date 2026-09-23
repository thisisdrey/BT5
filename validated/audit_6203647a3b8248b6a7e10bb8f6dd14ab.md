### Title
Unbounded varint decode in `pcValue`/`step`/`readvarint` causes index-out-of-range panic on malformed Go object files - (File: src/cmd/internal/objfile/goobj.go)

### Summary
`readvarint` in `src/cmd/internal/objfile/goobj.go` decodes a LEB128-style varint from a `[]byte` slice by repeatedly reading `s[0]` and advancing `s = s[1:]` inside an unbounded loop that only terminates when it sees a byte with the continuation bit (`0x80`) cleared. If the underlying slice runs out of bytes while the continuation bit is still set (or is empty/truncated), the next `s[0]` access panics with an index-out-of-range error, closely mirroring the CVE-2017-7716 pattern where `read_u32_leb128` walks past the end of a WASM-derived buffer without a length check.

### Finding Description
Attacker input: a crafted/corrupted Go object file (`.o`) or archive (`.a`) fed to `cmd/internal/objfile`'s Go-object parser. Entry point: `openGoFile` [1](#0-0)  parses the archive/object via `archive.Parse` and later `PCToLine` is invoked to symbolize a PC using the `AuxPcfile`/`AuxPcline` auxiliary symbol data extracted straight from the object's data section [2](#0-1) . That raw byte slice is passed unvalidated into `pcValue`, which calls `step`, which calls `readvarint` [3](#0-2) . `readvarint` reads `s[0]` and slices `s = s[1:]` in a loop bounded only by the terminator bit of the byte itself, with **no check that `len(s) > 0`** before indexing: [4](#0-3) . A pcline/pcfile aux-symbol payload that ends with a byte having the high bit set (or is truncated to zero length) drives `s` to empty and the next `s[0]` access panics. This is the same bug class as the CVE: a length/continuation-driven byte-stream decoder that walks off the end of an attacker-supplied buffer without bounds checking.

### Impact Explanation
The failure surfaces as an unrecovered Go runtime panic (`runtime error: index out of range [0] with length 0`) when a user runs `go tool objdump`, `go tool nm`, `addr2line`, or `go tool pprof` (which uses `cmd/internal/objfile`, per the grep results showing `nm.go`, `objdump/main.go`, `pprof/pprof.go`, `addr2line/main.go` as callers) against a maliciously crafted `.o`/`.a` file. This is a denial-of-service of the invoked tool via a crafted local input file — a plausible malicious-input parser panic. It does not achieve memory disclosure (Go's bounds-checked slices convert the C-style heap over-read into a safe panic), so there is no confidentiality/integrity/authentication impact. Under Go's security triage this would sit at most on the PUBLIC track as a low-severity DoS in a developer tool, not a PRIVATE/URGENT-track vulnerability, since it requires the victim to deliberately run one of these analysis tools against an untrusted binary/object file they chose to inspect (analogous to radare2 users opening a crafted file), which is a narrower, developer-tool-only workflow rather than an unauthenticated network-facing path.

### Likelihood Explanation
Requires a victim to run `go tool objdump`, `go tool nm`, `addr2line`, or `go tool pprof` on an attacker-supplied `.o`/`.a` file or archive containing a crafted `AuxPcfile`/`AuxPcline` byte table. This is a realistic but limited "open this file with the tool" workflow (similar to radare2 users loading a crafted WASM binary), not a remote/unauthenticated network attack surface.

### Recommendation
Add explicit length checks in `readvarint` (and `step`/`pcValue`) before indexing into the remaining slice, returning an error/sentinel instead of panicking when the table is truncated or the continuation bit runs past the end of the buffer, mirroring the bounds checks already present in `encoding/binary.ReadUvarint` and `debug/dwarf`'s `buf.varint()` (which safely returns `0, 0` on underflow) [5](#0-4) .

### Proof of Concept
```go
package objfile

import "testing"

func TestReadvarintOOBPanic(t *testing.T) {
    defer func() {
        if r := recover(); r == nil {
            t.Fatal("expected panic on truncated varint, got none")
        }
    }()
    // continuation bit set, no following byte: malformed pcline/pcfile table
    tab := []byte{0x80}
    p := &tab
    readvarint(p) // panics: index out of range [0] with length 0
}
```
Expected: the call panics with an index-out-of-range error instead of returning a decode error, demonstrating the unbounded-read defect analogous to CVE-2017-7716's `read_u32_leb128`. [4](#0-3)

### Citations

**File:** src/cmd/internal/objfile/goobj.go (L29-47)
```go
func openGoFile(f *os.File) (*File, error) {
	a, err := archive.Parse(f, false)
	if err != nil {
		return nil, err
	}
	entries := make([]*Entry, 0, len(a.Entries))
L:
	for _, e := range a.Entries {
		switch e.Type {
		case archive.EntryPkgDef, archive.EntrySentinelNonObj:
			continue
		case archive.EntryGoObj:
			o := e.Obj
			b := make([]byte, o.Size)
			_, err := f.ReadAt(b, o.Offset)
			if err != nil {
				return nil, err
			}
			r := goobj.NewReaderFromBytes(b, false)
```

**File:** src/cmd/internal/objfile/goobj.go (L254-270)
```go
		var pcfileSym, pclineSym goobj.SymRef
		for _, a := range r.Auxs(i) {
			switch a.Type() {
			case goobj.AuxPcfile:
				pcfileSym = a.Sym()
			case goobj.AuxPcline:
				pclineSym = a.Sym()
			}
		}
		if pcfileSym.IsZero() || pclineSym.IsZero() {
			continue
		}
		pcline := getSymData(pclineSym)
		line := int(pcValue(pcline, pc-addr, f.arch))
		pcfile := getSymData(pcfileSym)
		fileID := pcValue(pcfile, pc-addr, f.arch)
		fileName := r.File(int(fileID))
```

**File:** src/cmd/internal/objfile/goobj.go (L278-307)
```go
// pcValue looks up the given PC in a pc value table. target is the
// offset of the pc from the entry point.
func pcValue(tab []byte, target uint64, arch *sys.Arch) int32 {
	val := int32(-1)
	var pc uint64
	for step(&tab, &pc, &val, pc == 0, arch) {
		if target < pc {
			return val
		}
	}
	return -1
}

// step advances to the next pc, value pair in the encoded table.
func step(p *[]byte, pc *uint64, val *int32, first bool, arch *sys.Arch) bool {
	uvdelta := readvarint(p)
	if uvdelta == 0 && !first {
		return false
	}
	if uvdelta&1 != 0 {
		uvdelta = ^(uvdelta >> 1)
	} else {
		uvdelta >>= 1
	}
	vdelta := int32(uvdelta)
	pcdelta := readvarint(p) * uint32(arch.MinLC)
	*pc += uint64(pcdelta)
	*val += vdelta
	return true
}
```

**File:** src/cmd/internal/objfile/goobj.go (L309-323)
```go
// readvarint reads, removes, and returns a varint from *p.
func readvarint(p *[]byte) uint32 {
	var v, shift uint32
	s := *p
	for shift = 0; ; shift += 7 {
		b := s[0]
		s = s[1:]
		v |= (uint32(b) & 0x7F) << shift
		if b&0x80 == 0 {
			break
		}
	}
	*p = s
	return v
}
```

**File:** src/debug/dwarf/buf.go (L131-145)
```go
// Read a varint, which is 7 bits per byte, little endian.
// the 0x80 bit means read another byte.
func (b *buf) varint() (c uint64, bits uint) {
	for i := 0; i < len(b.data); i++ {
		byte := b.data[i]
		c |= uint64(byte&0x7F) << bits
		bits += 7
		if byte&0x80 == 0 {
			b.off += Offset(i + 1)
			b.data = b.data[i+1:]
			return c, bits
		}
	}
	return 0, 0
}
```

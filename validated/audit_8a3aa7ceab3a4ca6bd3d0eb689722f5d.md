No vulnerability found for this question.

**Rationale:** CVE-2022-23124 describes a C-language buffer over-read in Netatalk's `get_finderinfo` AFP (Apple Filing Protocol) implementation, where unvalidated attacker-supplied length data causes a read past the end of an allocated heap buffer, disclosing adjacent memory. This bug class is specific to manual memory management in C.

The `go--008` repository is the Go standard library and toolchain, which is memory-safe by construction — slice indexing and length-prefixed decoding in Go panics on out-of-bounds access rather than silently reading adjacent heap memory. I searched the closest analogous areas (length-prefixed binary parsers reachable from untrusted input, e.g. `crypto/tls` handshake/ECH parsing [1](#0-0) , `encoding/asn1` field parsing [2](#0-1) , `encoding/gob` decoding [3](#0-2) , HTTP/2 frame parsing [4](#0-3) , and `debug/elf`/`debug/dwarf` object parsing [5](#0-4) [6](#0-5) ). In each of these, length fields are validated against actual slice bounds before use, and any violation results in a Go panic/error return rather than an out-of-bounds heap read that discloses adjacent memory. `debug/elf` and `debug/dwarf` explicitly document that they are out of scope for the Go security policy for malformed/untrusted input [7](#0-6) .

There is no reachable, unauthenticated production Go code path exhibiting the same root cause (unchecked length leading to a heap over-read that discloses cross-request/cross-user memory) as CVE-2022-23124. Stretching this analogy to any of the Go parsers found would not be a genuine match, so I report none.

### Citations

**File:** src/crypto/tls/ech.go (L56-68)
```go
func parseECHConfig(enc []byte) (skip bool, ec echConfig, err error) {
	s := cryptobyte.String(enc)
	ec.raw = enc
	if !s.ReadUint16(&ec.Version) {
		return false, echConfig{}, &echConfigErr{"version"}
	}
	if !s.ReadUint16(&ec.Length) {
		return false, echConfig{}, &echConfigErr{"length"}
	}
	if len(ec.raw) < int(ec.Length)+4 {
		return false, echConfig{}, &echConfigErr{"length"}
	}
	ec.raw = ec.raw[:int(ec.Length)+4]
```

**File:** src/encoding/asn1/asn1.go (L701-705)
```go
// invalidLength reports whether offset + length > sliceLength, or if the
// addition would overflow.
func invalidLength(offset, length, sliceLength int) bool {
	return offset+length < offset || offset+length > sliceLength
}
```

**File:** src/encoding/gob/decode.go (L144-161)
```go
// decodeUint reads an encoded unsigned integer from state.r.
// Does not check for overflow.
func (state *decoderState) decodeUint() (x uint64) {
	b, err := state.b.ReadByte()
	if err != nil {
		error_(err)
	}
	if b <= 0x7f {
		return uint64(b)
	}
	n := -int(int8(b))
	if n > uint64Size {
		error_(errBadUint)
	}
	buf := state.b.Bytes()
	if len(buf) < n {
		errorf("invalid uint data length %d: exceeds input size %d", n, len(buf))
	}
```

**File:** src/net/http/internal/http2/frame.go (L1696-1708)
```go
func readByte(p []byte) (remain []byte, b byte, err error) {
	if len(p) == 0 {
		return nil, 0, io.ErrUnexpectedEOF
	}
	return p[1:], p[0], nil
}

func readUint32(p []byte) (remain []byte, v uint32, err error) {
	if len(p) < 4 {
		return nil, 0, io.ErrUnexpectedEOF
	}
	return p[4:], binary.BigEndian.Uint32(p[:4]), nil
}
```

**File:** src/debug/elf/file.go (L6-15)
```go
Package elf implements access to ELF object files.

# Security

This package is not designed to be hardened against adversarial inputs, and is
outside the scope of https://go.dev/security/policy. In particular, only basic
validation is done when parsing object files. As such, care should be taken when
parsing untrusted inputs, as parsing malformed files may consume significant
resources, or cause panics.
*/
```

**File:** src/debug/dwarf/entry.go (L410-421)
```go
func (b *buf) entry(cu *Entry, u *unit) *Entry {
	atab, ubase, vers := u.atable, u.base, u.vers
	off := b.off
	id := uint32(b.uint())
	if id == 0 {
		return &Entry{}
	}
	a, ok := atab[id]
	if !ok {
		b.error("unknown abbreviation table index")
		return nil
	}
```

### Title
Quadratic-time OID string conversion via unbounded big.Int growth in crypto/x509 - (File: src/crypto/x509/oid.go)

### Summary
`crypto/x509` parses X.509 `Certificate Policies` and other extension OIDs into the `OID` type via `newOIDFromDER`, and exposes a `String()` method used to render OIDs (e.g. for the `Policies []OID` field and other extension display/logging paths). When a single ASN.1 sub-identifier is encoded using an unusually large number of base-128 continuation bytes, `OID.String()` degrades into a `math/big.Int`-driven algorithm whose cost grows quadratically with the length of that sub-identifier, mirroring the exact algorithmic flaw described in CVE-2023-2650 for OpenSSL's `OBJ_obj2txt()`.

### Finding Description
An attacker supplies a certificate (or any DER-encoded structure feeding an OID) containing an OBJECT IDENTIFIER whose last sub-identifier is encoded across a very large number of continuation octets (each with the high bit set). `newOIDFromDER` [1](#0-0)  accepts this without any bound on a single sub-identifier's byte length — it only checks minimal encoding, not size. When the resulting `OID` value is later rendered via `OID.String()` [2](#0-1) , the method switches into a "big" path once the accumulated 64-bit value overflows: for every remaining byte of that sub-identifier it performs `bigVal = bigVal.Lsh(bigVal, bitsPerByte).Or(bigVal, ...)` [3](#0-2) . Each `Lsh` call must reallocate and shift the growing `big.Int`, so processing `k` continuation bytes for one arc costs O(k²) rather than O(k) — precisely the pathological growth pattern OpenSSL's advisory describes for `OBJ_obj2txt()`. This is a distinct implementation from `encoding/asn1`'s `ObjectIdentifier.String()` [4](#0-3) , which stores components as bounded 31-bit `int`s (enforced by `parseBase128Int`'s 5-byte/`MaxInt32` limit [5](#0-4) ) and is therefore not vulnerable to this specific big-integer blowup — the risk is isolated to the newer `crypto/x509.OID` type, which was added to support 64-bit-and-larger OID components.

### Impact Explanation
Calling `OID.String()` on such a crafted OID causes CPU time to scale quadratically with the attacker-chosen sub-identifier length, which can be made arbitrarily long within an ordinary DER structure (e.g., inside a certificate's policy or extension OID) — a CPU-based denial-of-service on any code path that formats/logs/displays parsed OIDs (matching OpenSSL's own "affects display of diverse objects" classification for this CVE). This aligns with Go's PUBLIC track for a low/medium algorithmic-complexity issue rather than an URGENT one, since exploitation requires the victim to actually call `.String()` on attacker-supplied OID data (a common but not universal certificate-processing behavior).

### Likelihood Explanation
Any application or middleware that parses attacker-supplied certificates or ASN.1 structures and subsequently renders extension/policy OIDs as text (logging, UI display, audit tooling, certificate inspection tools) is exposed. Because `newOIDFromDER` does not cap a sub-identifier's encoded length, an unprivileged party (e.g., a client presenting a certificate, or the source of any parsed certificate) can trigger the slow path with a comparatively small payload.

### Recommendation
Bound the number of continuation bytes accepted per sub-identifier in `newOIDFromDER` (or in `OID.String()`) to a sane maximum (e.g., a few hundred bytes, enough for any real-world OID), rejecting or short-circuiting oversized sub-identifiers before invoking the `big.Int` path, similar to the size limits OpenSSL ultimately adopted.

### Proof of Concept
```go
package x509

import (
	"strings"
	"testing"
	"time"
)

func TestOIDStringQuadraticBlowup(t *testing.T) {
	// Build a DER-encoded OID whose final sub-identifier has a very large
	// number of base-128 continuation bytes (high bit set), forcing the
	// big.Int overflow path in OID.String().
	der := []byte{0x2a} // first byte: valid start (1.2)
	n := 200000
	for i := 0; i < n; i++ {
		der = append(der, 0x80) // continuation byte, all zero payload bits
	}
	der = append(der, 0x01) // terminating byte

	oid, ok := newOIDFromDER(der)
	if !ok {
		t.Fatalf("expected newOIDFromDER to accept crafted OID")
	}

	start := time.Now()
	s := oid.String()
	elapsed := time.Since(start)

	if !strings.HasPrefix(s, "1.2.") {
		t.Fatalf("unexpected OID string: %q", s)
	}
	// On a machine where this scales linearly, this should take
	// microseconds/low milliseconds; with the quadratic big.Int path,
	// n=200000 continuation bytes takes seconds, demonstrating O(n^2) cost.
	if elapsed > 2*time.Second {
		t.Fatalf("OID.String() took %v for %d-byte OID; expected sub-second if linear", elapsed, n)
	}
}
```
Expected assertion failure/behavior: the test's wall-clock timing shows non-linear (multi-second) growth as `n` increases (e.g., doubling `n` roughly quadruples runtime), confirming the O(n²) `big.Int` shift-and-or pattern rather than the expected O(n) behavior.

### Citations

**File:** src/crypto/x509/oid.go (L33-52)
```go
func newOIDFromDER(der []byte) (OID, bool) {
	if len(der) == 0 || der[len(der)-1]&0x80 != 0 {
		return OID{}, false
	}

	start := 0
	for i, v := range der {
		// ITU-T X.690, section 8.19.2:
		// The subidentifier shall be encoded in the fewest possible octets,
		// that is, the leading octet of the subidentifier shall not have the value 0x80.
		if i == start && v == 0x80 {
			return OID{}, false
		}
		if v&0x80 == 0 {
			start = i + 1
		}
	}

	return OID{der}, true
}
```

**File:** src/crypto/x509/oid.go (L289-356)
```go
// String returns the string representation of the Object Identifier.
func (oid OID) String() string {
	var b strings.Builder
	b.Grow(32)
	const (
		valSize         = 64 // size in bits of val.
		bitsPerByte     = 7
		maxValSafeShift = (1 << (valSize - bitsPerByte)) - 1
	)
	var (
		start    = 0
		val      = uint64(0)
		numBuf   = make([]byte, 0, 21)
		bigVal   *big.Int
		overflow bool
	)
	for i, v := range oid.der {
		curVal := v & 0x7F
		valEnd := v&0x80 == 0
		if valEnd {
			if start != 0 {
				b.WriteByte('.')
			}
		}
		if !overflow && val > maxValSafeShift {
			if bigVal == nil {
				bigVal = new(big.Int)
			}
			bigVal = bigVal.SetUint64(val)
			overflow = true
		}
		if overflow {
			bigVal = bigVal.Lsh(bigVal, bitsPerByte).Or(bigVal, big.NewInt(int64(curVal)))
			if valEnd {
				if start == 0 {
					b.WriteString("2.")
					bigVal = bigVal.Sub(bigVal, big.NewInt(80))
				}
				numBuf = bigVal.Append(numBuf, 10)
				b.Write(numBuf)
				numBuf = numBuf[:0]
				val = 0
				start = i + 1
				overflow = false
			}
			continue
		}
		val <<= bitsPerByte
		val |= uint64(curVal)
		if valEnd {
			if start == 0 {
				if val < 80 {
					b.Write(strconv.AppendUint(numBuf, val/40, 10))
					b.WriteByte('.')
					b.Write(strconv.AppendUint(numBuf, val%40, 10))
				} else {
					b.WriteString("2.")
					b.Write(strconv.AppendUint(numBuf, val-80, 10))
				}
			} else {
				b.Write(strconv.AppendUint(numBuf, val, 10))
			}
			val = 0
			start = i + 1
		}
	}
	return b.String()
}
```

**File:** src/encoding/asn1/asn1.go (L233-246)
```go
func (oi ObjectIdentifier) String() string {
	var s strings.Builder
	s.Grow(32)

	buf := make([]byte, 0, 19)
	for i, v := range oi {
		if i > 0 {
			s.WriteByte('.')
		}
		s.Write(strconv.AppendInt(buf, int64(v), 10))
	}

	return s.String()
}
```

**File:** src/encoding/asn1/asn1.go (L299-329)
```go
// parseBase128Int parses a base-128 encoded int from the given offset in the
// given byte slice. It returns the value and the new offset.
func parseBase128Int(bytes []byte, initOffset int) (ret, offset int, err error) {
	offset = initOffset
	var ret64 int64
	for shifted := 0; offset < len(bytes); shifted++ {
		// 5 * 7 bits per byte == 35 bits of data
		// Thus the representation is either non-minimal or too large for an int32
		if shifted == 5 {
			err = StructuralError{"base 128 integer too large"}
			return
		}
		ret64 <<= 7
		b := bytes[offset]
		// integers should be minimally encoded, so the leading octet should
		// never be 0x80
		if shifted == 0 && b == 0x80 {
			err = SyntaxError{"integer is not minimally encoded"}
			return
		}
		ret64 |= int64(b & 0x7f)
		offset++
		if b&0x80 == 0 {
			ret = int(ret64)
			// Ensure that the returned value fits in an int on all platforms
			if ret64 > math.MaxInt32 {
				err = StructuralError{"base 128 integer too large"}
			}
			return
		}
	}
```

### Title
Out-of-bounds slice panic in `math/rand/v2.ChaCha8.UnmarshalBinary` from unvalidated length-prefixed buffer - (File: src/math/rand/v2/chacha8.go)

### Summary
`ChaCha8.UnmarshalBinary` decodes an untrusted length-prefixed "readbuf:" segment via `readUint8LengthPrefixed`, which allows the embedded buffer length byte to be any value 0-255. This length is never checked against the fixed 8-byte `readBuf` array before being used to compute a slice start index, leading to a negative-index slice-bounds panic when the attacker supplies a length greater than 8. This mirrors the Wireshark MSMMS class of bug: an attacker-supplied length field is trusted and used directly as an offset/size into a fixed-size buffer without validating it fits.

### Finding Description
Untrusted bytes reach `(*ChaCha8).UnmarshalBinary(data []byte)` at [1](#0-0) . After stripping the `"readbuf:"` prefix, `readUint8LengthPrefixed` extracts `buf` whose length is controlled entirely by the attacker-supplied length byte `b[0]` (0-255), with the only check being that the overall input is long enough to contain that many bytes: [2](#0-1) . The returned `buf` is then used as: `c.readLen = copy(c.readBuf[len(c.readBuf)-len(buf):], buf)` at [3](#0-2) . Since `c.readBuf` is a fixed `[8]byte` field [4](#0-3) , any `len(buf) > 8` makes `len(c.readBuf)-len(buf)` negative, and slicing a Go array/slice with a negative start index panics at runtime ("slice bounds out of range"). There is no check anywhere in `UnmarshalBinary` that `len(buf) <= len(c.readBuf)` before this computation.

### Impact Explanation
This causes an unrecoverable runtime panic (denial of service) in any process that calls `UnmarshalBinary` on attacker-influenced data — for example, an application that persists/restores PRNG state (via `encoding.BinaryUnmarshaler`, or indirectly through `gob`/other serializers that call it) and later reloads state supplied or influenced by a remote/untrusted party. Under Go's vulnerability triage, a reliably attacker-triggerable panic from malformed input processed by a standard-library parsing routine qualifies for a PUBLIC-track fix, similar to how Wireshark's bug was a heap overflow from an unchecked length field; here Go's bounds-checked runtime converts the equivalent primitive into a panic rather than memory corruption, but the root cause — trusting an attacker length field against a fixed buffer — is the same.

### Likelihood Explanation
Any code path that calls `(*ChaCha8).UnmarshalBinary` (directly, or via `encoding` framework helpers) on bytes that include or are derived from user-controlled input satisfies the "ordinary user data consumed by a normal victim workflow" premise — no privileged access, authentication, or malicious server is required, only a byte slice reaching this method.

### Recommendation
In `readUint8LengthPrefixed` or in `UnmarshalBinary`, validate that the parsed buffer length does not exceed `len(c.readBuf)` (8) before slicing; return the existing "invalid ChaCha8 Read buffer encoding" error instead of proceeding when `len(buf) > len(c.readBuf)`.

### Proof of Concept
```go
package rand_test

import (
	"testing"

	rand "math/rand/v2"
)

func TestChaCha8UnmarshalBinaryOverflow(t *testing.T) {
	c := rand.NewChaCha8([32]byte{})

	// Craft "readbuf:" + length byte 200 (> len(readBuf)==8) + 200 arbitrary bytes.
	data := append([]byte("readbuf:"), byte(200))
	data = append(data, make([]byte, 200)...)

	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic due to out-of-range slice index, got none")
		} else {
			t.Logf("got expected panic: %v", r)
		}
	}()

	_ = c.UnmarshalBinary(data) // panics: slice bounds out of range [-192:]
}
```
Expected result: the call panics with a "slice bounds out of range" runtime error instead of returning a decode error, confirming the unvalidated length-prefixed field is used directly against the fixed 8-byte buffer.

### Citations

**File:** src/math/rand/v2/chacha8.go (L15-21)
```go
type ChaCha8 struct {
	state chacha8rand.State

	// The last readLen bytes of readBuf are still to be consumed by Read.
	readBuf [8]byte
	readLen int // 0 <= readLen <= 8
}
```

**File:** src/math/rand/v2/chacha8.go (L73-85)
```go
// UnmarshalBinary implements the [encoding.BinaryUnmarshaler] interface.
func (c *ChaCha8) UnmarshalBinary(data []byte) error {
	data, ok := cutPrefix(data, []byte("readbuf:"))
	if ok {
		var buf []byte
		buf, data, ok = readUint8LengthPrefixed(data)
		if !ok {
			return errors.New("invalid ChaCha8 Read buffer encoding")
		}
		c.readLen = copy(c.readBuf[len(c.readBuf)-len(buf):], buf)
	}
	return chacha8rand.Unmarshal(&c.state, data)
}
```

**File:** src/math/rand/v2/chacha8.go (L94-99)
```go
func readUint8LengthPrefixed(b []byte) (buf, rest []byte, ok bool) {
	if len(b) == 0 || len(b) < int(1+b[0]) {
		return nil, nil, false
	}
	return b[1 : 1+b[0]], b[1+b[0]:], true
}
```

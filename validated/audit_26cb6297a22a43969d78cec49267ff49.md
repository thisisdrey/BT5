### Title
Out-of-bounds slice access in `suffixarray.Index.Read` from unvalidated length field - (File: `src/index/suffixarray/suffixarray.go`)

### Summary
`Index.Read` deserializes a suffix-array index from an `io.Reader` and, like the Contiki-NG `read_frame` bug, reads an attacker-controlled length field and uses it to index into a fixed-size buffer without checking that the length fits within that buffer's capacity. The result is a reachable out-of-bounds slice-bounds panic when `Index.Read` is used to load an untrusted/corrupted serialized index (the documented use case being loading a previously-saved index, e.g. from disk or network, analogous to a victim receiving crafted "radio frames").

### Finding Description
`Index.Read(r io.Reader)` allocates `buf := make([]byte, bufSize)` with `bufSize = 16 << 10` and calls `readSlice(r, buf, sa, uint64(n))` in a loop [1](#0-0) . Inside `readSlice`, the size of the encoded chunk is read from the untrusted stream via `readInt`, and only validated to be a non-negative value representable as `int`:
```go
size64, err = readInt(r, buf)
if int64(int(size64)) != size64 || int(size64) < 0 {
    return 0, errCorrupted
}
size := int(size64)
if _, err = io.ReadFull(r, buf[binary.MaxVarintLen64:size]); err != nil {
    return
}
``` [2](#0-1) 

Unlike the `read_frame` fix pattern (validate length against MTU AND against the destination buffer capacity), `size` here is never checked against `len(buf)` (the fixed `bufSize` allocated by the caller). Any `size` value between `binary.MaxVarintLen64` and `math.MaxInt` that exceeds `bufSize` will cause `buf[binary.MaxVarintLen64:size]` to panic with "slice bounds out of range" the moment a crafted serialized index (attacker-supplied bytes) is parsed via `(*Index).Read`.

### Impact Explanation
Go's runtime slice-bounds checking prevents actual memory corruption (unlike the C-based Contiki-NG driver), but this still yields an unrecoverable panic triggered purely by parsing attacker-supplied data through a public, documented API (`Index.Read`), which is the closest Go analog to the reported out-of-bounds-write primitive. Under Go's vulnerability policy, a parser panic on malicious input in production code (not test/mock/vendored) can qualify as a PUBLIC-track issue if `Index.Read` is reachable on untrusted data in a normal workflow.

### Likelihood Explanation
`Index` is a public type intended to persist/restore suffix-array indexes (see doc comment for `New`/`Read`/`Write`) [3](#0-2) ; any application that serializes an index and later reloads it from a source not fully trusted (e.g., a cache file, a downloaded artifact, or data received over a network) exercises this exact code path with a single call, requiring no special privileges — matching the "unprivileged data consumer" premise.

### Recommendation
In `readSlice`, validate `size` against `len(buf)` (and also `size >= binary.MaxVarintLen64`) before slicing, returning `errCorrupted` if `size > len(buf)`, mirroring the same defensive check already applied to the suffix-array index bounds (`x >= maxIndex`) a few lines below.

### Proof of Concept
```go
package suffixarray_test

import (
	"bytes"
	"encoding/binary"
	"index/suffixarray"
	"testing"
)

// TestReadOOBPanic demonstrates that Index.Read panics with a slice-bounds
// error when fed a crafted stream whose encoded chunk-size field exceeds
// the internal read buffer (16<<10 bytes), because readSlice never checks
// size against len(buf).
func TestReadOOBPanic(t *testing.T) {
	var buf bytes.Buffer

	// n: length of data (small, valid)
	varint := make([]byte, binary.MaxVarintLen64)
	writeVarint := func(x int64) {
		nn := binary.PutVarint(varint, x)
		buf.Write(varint[:binary.MaxVarintLen64])
		_ = nn
	}
	writeVarint(4) // len(data) = 4

	buf.WriteString("abcd") // data bytes

	// size field for readSlice: larger than bufSize (16<<10 = 16384)
	writeVarint(1 << 20) // 1MB - forces buf[10:size] out of range (buf is 16384 bytes)

	defer func() {
		if r := recover(); r == nil {
			t.Fatalf("expected panic due to out-of-range slice, got none")
		} else {
			t.Logf("got expected panic: %v", r)
		}
	}()

	ix := new(suffixarray.Index)
	_ = ix.Read(&buf) // should panic: "slice bounds out of range"
}
```
Expected result: the call to `ix.Read` panics with a runtime `slice bounds out of range` error instead of returning the documented `errCorrupted`/`error` value, confirming that the length field from untrusted serialized input is used to index into the fixed-size internal buffer without validating it against the buffer's capacity.

### Citations

**File:** src/index/suffixarray/suffixarray.go (L5-16)
```go
// Package suffixarray implements substring search in logarithmic time using
// an in-memory suffix array.
//
// Example use:
//
//	// create index for some data
//	index := suffixarray.New(data)
//
//	// lookup byte slice s
//	offsets1 := index.Lookup(s, -1) // the list of all indices where s occurs in data
//	offsets2 := index.Lookup(s, 3)  // the list of at most 3 indices where s occurs in data
package suffixarray
```

**File:** src/index/suffixarray/suffixarray.go (L122-140)
```go
// readSlice reads data[:n] from r and returns n; maxIndex is the length of the suffix array.
// It uses buf to buffer the read.
func readSlice(r io.Reader, buf []byte, data ints, maxIndex uint64) (n int, err error) {
	// read buffer size
	var size64 int64
	size64, err = readInt(r, buf)
	if err != nil {
		return
	}
	if int64(int(size64)) != size64 || int(size64) < 0 {
		// We never write chunks this big anyway.
		return 0, errCorrupted
	}
	size := int(size64)

	// read buffer w/o the size
	if _, err = io.ReadFull(r, buf[binary.MaxVarintLen64:size]); err != nil {
		return
	}
```

**File:** src/index/suffixarray/suffixarray.go (L160-209)
```go
const bufSize = 16 << 10 // reasonable for BenchmarkSaveRestore

// Read reads the index from r into x; x must not be nil.
func (x *Index) Read(r io.Reader) error {
	// buffer for all reads
	buf := make([]byte, bufSize)

	// read length
	n64, err := readInt(r, buf)
	if err != nil {
		return err
	}
	if int64(int(n64)) != n64 || int(n64) < 0 {
		return errCorrupted
	}
	n := int(n64)

	// allocate space
	if 2*n < cap(x.data) || cap(x.data) < n || x.sa.int32 != nil && n > maxData32 || x.sa.int64 != nil && n <= maxData32 {
		// new data is significantly smaller or larger than
		// existing buffers - allocate new ones
		x.data = make([]byte, n)
		x.sa.int32 = nil
		x.sa.int64 = nil
		if n <= maxData32 {
			x.sa.int32 = make([]int32, n)
		} else {
			x.sa.int64 = make([]int64, n)
		}
	} else {
		// re-use existing buffers
		x.data = x.data[0:n]
		x.sa = x.sa.slice(0, n)
	}

	// read data
	if _, err := io.ReadFull(r, x.data); err != nil {
		return err
	}

	// read index
	sa := x.sa
	for sa.len() > 0 {
		n, err := readSlice(r, buf, sa, uint64(n))
		if err != nil {
			return err
		}
		sa = sa.slice(n, sa.len())
	}
	return nil
```

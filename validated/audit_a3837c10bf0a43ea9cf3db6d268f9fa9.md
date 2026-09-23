Based on my analysis, this CVE does not have a valid analog in the Go standard library's GIF/LZW decoder.

The CVE-2018-11490 report describes a C-language heap-based buffer overflow in giflib's `DGifDecompressLine`, caused by an unchecked `Private->RunningCode - 2` array index. The closest Go analog is `compress/lzw`'s `(*Reader).decode` [1](#0-0) , which is used by `image/gif`'s decoder via `lzw.NewReader` in `readImageDescriptor` [2](#0-1) .

However, the Go implementation does not share the root cause. The `suffix` and `prefix` arrays are statically sized to `[1 << maxWidth]` (4096 entries) [3](#0-2) , and every code used to index into them is bounds-checked against `r.hi`/`r.clear` before use in the `switch` statement (`code < r.clear`, `code == r.clear`, `code == r.eof`, `code <= r.hi`, `default: invalid code`) [4](#0-3) . The `hi` counter itself is bounded by `overflow` and capped at `maxWidth`, with explicit logic to prevent it from exceeding the array size [5](#0-4) . There is no equivalent of an unchecked "`RunningCode - 2`" index into a fixed buffer; any out-of-range code hits the `default` branch and returns `errors.New("lzw: invalid code")` instead of indexing out of bounds [6](#0-5) .

The `image/gif` package's own fuzz/test coverage (`reader_test.go`) and documented security-considerations comments [7](#0-6)  further indicate this decoder has already been hardened against malformed/adversarial GIF streams. No unchecked-index or heap-overflow primitive matching the CVE's root cause is reachable in this Go code path.

### No vulnerability found for this question.

### Citations

**File:** src/compress/lzw/reader.go (L74-75)
```go
	suffix [1 << maxWidth]uint8
	prefix [1 << maxWidth]uint16
```

**File:** src/compress/lzw/reader.go (L139-150)
```go
func (r *Reader) decode() {
	// Loop over the code stream, converting codes into decompressed bytes.
loop:
	for {
		code, err := r.read(r)
		if err != nil {
			if err == io.EOF {
				err = io.ErrUnexpectedEOF
			}
			r.err = err
			break
		}
```

**File:** src/compress/lzw/reader.go (L151-200)
```go
		switch {
		case code < r.clear:
			// We have a literal code.
			r.output[r.o] = uint8(code)
			r.o++
			if r.last != decoderInvalidCode {
				// Save what the hi code expands to.
				r.suffix[r.hi] = uint8(code)
				r.prefix[r.hi] = r.last
			}
		case code == r.clear:
			r.width = 1 + uint(r.litWidth)
			r.hi = r.eof
			r.overflow = 1 << r.width
			r.last = decoderInvalidCode
			continue
		case code == r.eof:
			r.err = io.EOF
			break loop
		case code <= r.hi:
			c, i := code, len(r.output)-1
			if code == r.hi && r.last != decoderInvalidCode {
				// code == hi is a special case which expands to the last expansion
				// followed by the head of the last expansion. To find the head, we walk
				// the prefix chain until we find a literal code.
				c = r.last
				for c >= r.clear {
					c = r.prefix[c]
				}
				r.output[i] = uint8(c)
				i--
				c = r.last
			}
			// Copy the suffix chain into output and then write that to w.
			for c >= r.clear {
				r.output[i] = r.suffix[c]
				i--
				c = r.prefix[c]
			}
			r.output[i] = uint8(c)
			r.o += copy(r.output[r.o:], r.output[i:])
			if r.last != decoderInvalidCode {
				// Save what the hi code expands to.
				r.suffix[r.hi] = uint8(c)
				r.prefix[r.hi] = r.last
			}
		default:
			r.err = errors.New("lzw: invalid code")
			break loop
		}
```

**File:** src/compress/lzw/reader.go (L201-216)
```go
		r.last, r.hi = code, r.hi+1
		if r.hi >= r.overflow {
			if r.hi > r.overflow {
				panic("unreachable")
			}
			if r.width == maxWidth {
				r.last = decoderInvalidCode
				// Undo the d.hi++ a few lines above, so that (1) we maintain
				// the invariant that d.hi < d.overflow, and (2) d.hi does not
				// eventually overflow a uint16.
				r.hi--
			} else {
				r.width++
				r.overflow = 1 << r.width
			}
		}
```

**File:** src/image/gif/reader.go (L1-11)
```go
// Copyright 2011 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

// Package gif implements a GIF image decoder and encoder.
//
// The GIF specification is at https://www.w3.org/Graphics/GIF/spec-gif89a.txt.
//
// When decoding untrusted input, read dimensions with [DecodeConfig] before
// calling [Decode] or [DecodeAll]; see those functions and the "Security
// Considerations" section in the [image] package documentation.
```

**File:** src/image/gif/reader.go (L428-431)
```go
	br := &blockReader{d: d}
	lzwr := lzw.NewReader(br, lzw.LSB, int(litWidth))
	defer lzwr.Close()
	if err = readFull(lzwr, m.Pix); err != nil {
```

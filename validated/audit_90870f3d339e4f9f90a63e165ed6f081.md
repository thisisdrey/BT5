I investigated whether Go's standard library has an analogous heap buffer overflow to kitty's `load_image_data()` PNG-handling bug (CVE-2026-33633), where a declared payload size exceeding an internal buffer's capacity caused a heap overflow.

**Findings:** The closest analog in this repository is the PNG decoder at `src/image/png/reader.go`. Unlike kitty's C buffer-growing logic, Go's decoder does not perform manual capacity tracking with unchecked memcpy-style writes:

- Row buffers (`cr`, `pr`) are sized from a validated `rowSize` computed with explicit overflow checks before allocation: [1](#0-0) 
- Dimensions from `IHDR` are validated against `int64`/`int32` overflow before any buffer is sized: [2](#0-1) 
- Pixel data is read via `io.ReadFull(r, cr)` into a fixed-size slice matching exactly `rowSize`, so a malicious/oversized `IDAT` payload cannot cause a write past the buffer — Go's slice bounds checking would instead raise a runtime panic if an index calculation were wrong, not silently corrupt heap memory: [3](#0-2) 
- Palette-index writes into `paletted.Pix` are bounds-checked/extended rather than blindly trusting untrusted palette indices: [4](#0-3) 
- The `IDAT` length itself is validated for negative/overflow conditions before being used to bound reads: [5](#0-4) 

The kitty vulnerability's root cause was a C-level buffer capacity mismatch (tracked length vs. actual allocated size) in a hand-rolled growable buffer — a class of bug that requires manual pointer/length bookkeeping. Go's slice/array primitives enforce bounds at every index and `copy`/`ReadFull` operation, so an equivalent "declared size exceeds buffer, write proceeds anyway" primitive does not exist in this decoder; a logic error here would produce a safe panic (denial-of-service at most, and only via a length mismatch bug that isn't present in the current code) rather than an exploitable heap corruption.

I found no reachable path in `image/png` (or other production PNG/graphics-protocol-like handling in this repo) that reproduces kitty's exact primitive: an attacker-controlled length declaration causing a write past a heap buffer's actual capacity.

#No vulnerability found for this question.

### Citations

**File:** src/image/png/reader.go (L163-171)
```go
	nPixels64 := int64(w) * int64(h)
	nPixels := int(nPixels64)
	if nPixels64 != int64(nPixels) {
		return UnsupportedError("dimension overflow")
	}
	// There can be up to 8 bytes per pixel, for 16 bits per channel RGBA.
	if nPixels != (nPixels*8)/8 {
		return UnsupportedError("dimension overflow")
	}
```

**File:** src/image/png/reader.go (L353-356)
```go
	if int(d.idatLength) < 0 {
		return 0, UnsupportedError("IDAT chunk length overflow")
	}
	n, err := d.r.Read(p[:min(len(p), int(d.idatLength))])
```

**File:** src/image/png/reader.go (L498-507)
```go
	bytesPerPixel := (bitsPerPixel + 7) / 8

	// The +1 is for the per-row filter type, which is at cr[0].
	rowSize := 1 + (int64(bitsPerPixel)*int64(width)+7)/8
	if rowSize != int64(int(rowSize)) {
		return nil, UnsupportedError("dimension overflow")
	}
	// cr and pr are the bytes for the current and previous row.
	cr := make([]uint8, rowSize)
	pr := make([]uint8, rowSize)
```

**File:** src/image/png/reader.go (L509-517)
```go
	for y := 0; y < height; y++ {
		// Read the decompressed bytes.
		_, err := io.ReadFull(r, cr)
		if err != nil {
			if err == io.EOF || err == io.ErrUnexpectedEOF {
				return nil, FormatError("not enough pixel data")
			}
			return nil, err
		}
```

**File:** src/image/png/reader.go (L711-720)
```go
		case cbP8:
			if len(paletted.Palette) != 256 {
				for x := 0; x < width; x++ {
					if len(paletted.Palette) <= int(cdat[x]) {
						paletted.Palette = paletted.Palette[:int(cdat[x])+1]
					}
				}
			}
			copy(paletted.Pix[pixOffset:], cdat)
			pixOffset += paletted.Stride
```

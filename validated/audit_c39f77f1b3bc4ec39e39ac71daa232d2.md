Based on my research, this maps to a known, non-issue category in Go's stdlib.

## Analysis

The CVE describes GraphicsMagick's `ReadMATImage()` allocating memory based on an attacker-controlled size field in a MATLAB image header, before validating that the file actually contains that much data — a generic OOM/resource-exhaustion bug class.

The closest analog in this repo is Go's `image` package family (`image/gif`, `image/jpeg`, `image/png`), which similarly parses header fields (width/height, MCU counts, etc.) and allocates buffers proportional to those declared dimensions before all pixel data is read or validated. For example, `png.readImagePass` allocates a full `image.NewRGBA`/`image.NewPaletted` etc. based on `d.width, d.height` from the IHDR chunk [1](#0-0) , and `jpeg` computes `mxx`/`myy` MCU counts and calls `d.makeImg` from header-declared dimensions before all scan data is consumed [2](#0-1) .

However, this exact behavior is explicitly documented and treated as expected, not a vulnerability, in Go's own package documentation: `image.Decode`'s doc comment states "Decoding may allocate memory proportional to the width and height in the image header before all pixel data is consumed or validated. When decoding untrusted input, call `DecodeConfig` first..." [3](#0-2) , and the package-level "Security Considerations" section repeats this same guidance [4](#0-3) . The repo even ships a runnable example (`ExampleDecode_untrusted`) demonstrating exactly this mitigation pattern of calling `DecodeConfig` and rejecting oversized dimensions before calling `Decode` [5](#0-4) , and `DecodeAll` in gif similarly documents the same caveat [6](#0-5) .

Per the scan rules, this class of finding is explicitly out of scope: "never ask about unbounded memory, huge input, or generic resource exhaustion," and reports must "reject... already-fixed bugs" — this is a documented, known, and mitigated design tradeoff in the Go image package (not a bug), with an explicit recommended and tested mitigation path already in the codebase. There is no distinct memory-safety failure, integrity/auth bypass, or code-execution primitive here — just the same generic "allocate before fully validating declared size" pattern that Go's docs already call out and provide a workaround for.

### No vulnerability found for this question.

### Citations

**File:** src/image/png/reader.go (L437-463)
```go
	switch d.cb {
	case cbG1, cbG2, cbG4, cbG8:
		bitsPerPixel = d.depth
		if d.useTransparent {
			nrgba = image.NewNRGBA(image.Rect(0, 0, width, height))
			img = nrgba
		} else {
			gray = image.NewGray(image.Rect(0, 0, width, height))
			img = gray
		}
	case cbGA8:
		bitsPerPixel = 16
		nrgba = image.NewNRGBA(image.Rect(0, 0, width, height))
		img = nrgba
	case cbTC8:
		bitsPerPixel = 24
		if d.useTransparent {
			nrgba = image.NewNRGBA(image.Rect(0, 0, width, height))
			img = nrgba
		} else {
			rgba = image.NewRGBA(image.Rect(0, 0, width, height))
			img = rgba
		}
	case cbP1, cbP2, cbP4, cbP8:
		bitsPerPixel = d.depth
		paletted = image.NewPaletted(image.Rect(0, 0, width, height), d.palette)
		img = paletted
```

**File:** src/image/jpeg/scan.go (L154-162)
```go
	// mxx and myy are the number of MCUs (Minimum Coded Units) in the image.
	// The MCU dimensions are based on the maximum sampling factors.
	// For standard subsampling, maxH/maxV equals h0/v0 (Y's factors).
	// For flex mode, Y may not have the maximum factors.
	mxx := (d.width + 8*d.maxH - 1) / (8 * d.maxH)
	myy := (d.height + 8*d.maxV - 1) / (8 * d.maxV)
	if d.img1 == nil && d.img3 == nil {
		d.makeImg(mxx, myy)
	}
```

**File:** src/image/format.go (L87-92)
```go
//
// Decoding may allocate memory proportional to the width and height in the
// image header before all pixel data is consumed or validated. When
// decoding untrusted input, call [DecodeConfig] first to inspect dimensions
// and reject images that would exceed resource limits; see the "Security
// Considerations" section in the [image] package documentation.
```

**File:** src/image/image.go (L25-30)
```go
// # Security Considerations
//
// The image package can be used to parse arbitrarily large images, which can
// cause resource exhaustion on machines which do not have enough memory to
// store them. When operating on arbitrary images, [DecodeConfig] should be called
// before [Decode], so that the program can decide whether the image, as defined
```

**File:** src/image/decode_example_test.go (L34-63)
```go
// ExampleDecode_untrusted demonstrates decoding an untrusted
// image file in two steps so that unexpectedly large
// memory allocations can be safely avoided.
func ExampleDecode_untrusted() {
	// This GIF data is a valid 1x1 image (layout matches package gif tests).
	gifData := []byte{
		'G', 'I', 'F', '8', '9', 'a',
		1, 0, 1, 0,
		128, 0, 0,
		0, 0, 0, 1, 1, 1,
		0x21, 0xf9, 0x04, 0x00, 0x00, 0x00, 0xff, 0x00,
		0x2c,
		0x00, 0x00, 0x00, 0x00,
		0x01, 0x00, 0x01, 0x00,
		0x00,
		0x02, 0x02, 0x4c, 0x01, 0x00,
		0x3b,
	}

	cfg, _, err := image.DecodeConfig(bytes.NewReader(gifData))
	if err != nil {
		log.Fatal(err)
	}

	// Use int64 to avoid overflow on 32-bit platforms.
	const maxPixels = 10000
	if int64(cfg.Width)*int64(cfg.Height) > maxPixels {
		fmt.Println("rejected: dimensions too large")
		return
	}
```

**File:** src/image/gif/reader.go (L615-618)
```go
// Like [Decode], this allocates a paletted buffer per frame from width and
// height in the image descriptors. [DecodeAll] retains every decoded frame in
// memory. For untrusted input, call [DecodeConfig] first to verify the
// logical screen size and reject inputs that would require excessive memory.
```

### Title
Unbounded slice-index underflow in `Bitmap.Unpack()` when parsing attacker-suppliable `.bitmap` bitmap data - (File: `internal/git/packfile/bitmap.go`)

### Summary
The Firedancer bug is a classic "attacker-controlled loop count vs. statically sized buffer" defect: a buffer is pre-sized under one assumption (a single item) while a loop driven by parsed, untrusted data writes past it. Gitaly's `Bitmap.Unpack()` in `internal/git/packfile/bitmap.go` contains the same structural flaw in Go form: the destination buffer is sized from a *declared* bit count (`e.bits`), but the loop that fills it walks run-lengths (`nClean`/`nDirty`) taken directly from attacker-controlled `.bitmap` bytes without ever checking them against the actually-allocated buffer size.

### Finding Description
`ReadEWAH()` reads two header fields, `e.bits` and `e.words`, straight from the file with only an `int32` overflow check [1](#0-0) , then reads `e.words*8+4` raw bytes into `e.raw` [2](#0-1) .

`Unpack()` then computes a destination buffer sized purely from `e.bits`:
```go
nUnpackedWords := e.bits / wordBits
...
buf := make([]byte, nUnpackedWords*wordSize)
bufPos := len(buf)
``` [3](#0-2) 

It then walks `e.raw` word-by-word, and for each EWAH "clean" or "dirty" run decrements `bufPos` by `wordSize` and writes into `buf[bufPos-wordSize:bufPos]`:
```go
for ; nClean > 0; nClean-- {
    if cleanBit == 1 {
        copy(buf[bufPos-wordSize:bufPos], fillOnes)
    }
    bufPos -= wordSize
}
for ; nDirty > 0; nDirty-- {
    copy(buf[bufPos-wordSize:bufPos], e.raw[wordSize*i:wordSize*(i+1)])
    bufPos -= wordSize
    i++
}
``` [4](#0-3) 

`nClean` and `nDirty` are derived from the 64-bit `header` word taken directly from `e.raw` (`header>>1` and `header>>33`), i.e. fully attacker-controlled, and are never validated against the number of words actually reserved in `buf` (`nUnpackedWords`) or against `e.words`. If the encoded run lengths sum to more than `nUnpackedWords`, `bufPos` underflows past zero and `buf[bufPos-wordSize:bufPos]` becomes a negative-bound slice expression, which Go turns into an unrecoverable `panic: slice bounds out of range` rather than a silent corruption (Go's runtime bounds checking prevents true heap corruption, unlike the C case in Firedancer). This is the same "declared size vs. actual write count are decoupled and unchecked" root cause as the Firedancer report, just manifesting as a crash instead of memory corruption due to Go's safety guarantees.

I was not able to fully confirm, given tool-call limits, whether `.bitmap` files can be delivered end-to-end through a specific ordinary-user RPC (e.g. `CreateRepositoryFromSnapshot`, which extracts a client-supplied tar archive into repository storage per `internal/gitaly/service/repository/create_repository_from_snapshot.go`) all the way to a call of `LoadBitmap()`/`Unpack()`, nor whether an outer panic-recovery interceptor exists in the gRPC server stack (no `recover()` was found under `internal/gitaly/server/`, but interceptor registration was not fully traced).

### Impact Explanation
If reachable from attacker-influenced repository data (a repository containing a crafted `.bitmap` pack index side-file, e.g. supplied via a snapshot/import/replication path that copies raw repository files), this causes an unrecovered Go panic in the RPC handler thread that invokes `LoadBitmap`/`Unpack` (used by repository info/statistics and housekeeping code paths per `internal/gitaly/service/repository/repository_info.go` and `internal/git/stats/repository_info.go`). Depending on whether the surrounding gRPC middleware recovers per-RPC panics, this is at minimum a denial of service of that specific handler, and at worst can crash the whole `gitaly-server` process, affecting all repositories served by that node.

### Likelihood Explanation
`.bitmap` files are not normally part of the standard git push/fetch/clone wire protocol, so the most direct trigger requires a path that allows raw file injection into repository storage (snapshot restore, disk-based replication/import, or a corrupted/attacker-influenced repack artifact). This narrows the practical likelihood compared to a pure protocol-level bug, but any workflow that copies or restores repository directories from a less-trusted source (backups, migrations, snapshot RPC) is a plausible vector.

### Recommendation
In `Bitmap.Unpack()`, validate before each write that `bufPos >= wordSize` (and that `i < e.words`) before decrementing/copying, returning an error such as `"invalid EWAH bitmap: run length exceeds allocated words"` instead of proceeding. Likewise, `ReadEWAH()` should cross-validate `e.words` and `e.bits` for consistency (e.g. ensure `e.words` is compatible with the sum of run lengths it is later asked to decode) before trusting header-derived run lengths, mirroring the fix needed in the original Firedancer report: never let the write-loop's iteration count be driven by untrusted data without checking it against the true bounds of the pre-allocated buffer.

### Proof of Concept
Not independently reproduced (no code execution tool available in this session). Conceptually: craft a `.bitmap` file whose header declares a small `bits`/`words` value (so `buf` in `Unpack()` is small) but whose EWAH word stream encodes a `nClean` or `nDirty` run count large enough that cumulative `bufPos -= wordSize` decrements drive `bufPos` below `wordSize`, triggering `buf[bufPos-wordSize:bufPos]` with a negative start index and a `slice bounds out of range` panic in `internal/git/packfile/bitmap.go`, function `(*Bitmap).Unpack`.

### Citations

**File:** internal/git/packfile/bitmap.go (L192-202)
```go
	uBits := binary.BigEndian.Uint32(header[:4])
	if uBits > math.MaxInt32 {
		return nil, fmt.Errorf("too many bits in bitmap: %d", uBits)
	}
	e.bits = int(uBits)

	uWords := binary.BigEndian.Uint32(header[4:])
	if uWords > math.MaxInt32 {
		return nil, fmt.Errorf("too many words in bitmap: %d", uWords)
	}
	e.words = int(uWords)
```

**File:** internal/git/packfile/bitmap.go (L204-216)
```go
	const ewahTrailerLen = 4
	rawSize := int64(e.words)*8 + ewahTrailerLen
	if rawSize > math.MaxInt32 {
		return nil, fmt.Errorf("bitmap does not fit in Go slice")
	}

	e.raw = make([]byte, int(rawSize))

	if _, err := io.ReadFull(r, e.raw); err != nil {
		return nil, err
	}

	return e, nil
```

**File:** internal/git/packfile/bitmap.go (L230-236)
```go
	nUnpackedWords := e.bits / wordBits
	if e.bits%wordBits > 0 {
		nUnpackedWords++
	}

	buf := make([]byte, nUnpackedWords*wordSize)
	bufPos := len(buf)
```

**File:** internal/git/packfile/bitmap.go (L248-268)
```go
		for ; nClean > 0; nClean-- {
			// If cleanBit == 0 we don't have to do anything, because each byte in
			// buf is initially zero.
			if cleanBit == 1 {
				copy(
					buf[bufPos-wordSize:bufPos],
					fillOnes,
				)
			}

			bufPos -= wordSize
		}

		for ; nDirty > 0; nDirty-- {
			copy(
				buf[bufPos-wordSize:bufPos],
				e.raw[wordSize*i:wordSize*(i+1)],
			)
			bufPos -= wordSize
			i++
		}
```

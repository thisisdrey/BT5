### Title
Memory over-allocation in EWAH bitmap unpacking via unvalidated `bits` header field - (File: internal/git/packfile/bitmap.go)

### Summary
`Bitmap.Unpack()` allocates a buffer sized from the bitmap's `bits` field read out of the `.bitmap` file header, but `bits` is never validated against the actual amount of compressed data (`words`/`raw`) that was read for that bitmap. This mirrors the evm-core `Memory::copy_large` flaw: a size value is used to drive an allocation without being tied to how much data is actually needed/available, allowing a small crafted input to trigger a disproportionately large allocation.

### Finding Description
`ReadEWAH` reads two independent 32-bit fields from the bitmap stream, `uBits` and `uWords`, each individually bounds-checked only against `math.MaxInt32`: [1](#0-0) 

`e.raw`'s allocation size is correctly derived from `e.words` (`rawSize := int64(e.words)*8 + 4`), and reading `e.raw` via `io.ReadFull` forces the attacker to actually supply that many bytes over the transport, so `words`/`raw` allocation is naturally cost-bound by how much data is sent.

However, `e.bits` is completely decoupled from `words`/`raw`. When `Unpack()` is later called, the output buffer size is computed purely from `e.bits`, independent of how much compressed data was actually supplied: [2](#0-1) 

Because `bits` is checked only against `math.MaxInt32` and not against `words` (the amount of real data backing it), an attacker can set `bits` to `math.MaxInt32` while keeping `words` (and thus the actual bytes that must be transmitted/read) tiny. This forces `Unpack()` to allocate `nUnpackedWords*wordSize` ≈ 256+ MiB from an 8-byte header plus a minimal `raw` payload — a classic allocate-more-than-needed amplification, structurally identical to the evm-core `copy_large` bug class (a size value drives allocation without being bound to the real amount of underlying data).

This code path is exercised by `IndexBitmap.LoadBitmap()`, which calls `ReadEWAH`/`Unpack()` four times unconditionally (`Commits`, `Trees`, `Blobs`, `Tags`) plus once per indexed commit for XOR-compressed commit bitmaps: [3](#0-2) 

`LoadBitmap` is invoked from repository statistics/housekeeping code paths (`internal/gitaly/service/repository/repository_info.go`, `internal/git/housekeeping/metrics.go`, `internal/git/stats/repository_info.go`), which run against `.bitmap` files present in a repository's `objects/pack` directory.

### Impact Explanation
If an attacker can get a crafted `.bitmap` file into a repository's pack directory (e.g. via a repository import/replication/fork flow, or any code path that copies raw pack/bitmap files from a repository the attacker controls) and subsequently trigger any RPC that computes repository info/statistics (which loads bitmaps), each triggering request can force one or more ~256+ MiB allocations for a trivially small crafted `.bitmap` file, and repeated calls can be used to exhaust Gitaly server memory — a denial-of-service condition consistent with the "Memory Allocation with Excessive Size Value" (CWE) class referenced in the report.

### Likelihood Explanation
Medium/uncertain. `.bitmap` files are normally produced only by trusted `git repack -b`/`git pack-objects --write-bitmap-index` invocations run by Gitaly itself, not accepted directly as push/fetch payload over the standard Git protocol, so the most direct "ordinary user push" path is not obviously reachable. The realistic trigger requires a workflow where Gitaly ingests or copies a repository's raw on-disk object database from a source an attacker influences (e.g. repository import/replication/fork of an attacker-hosted source, or a maliciously crafted repository snapshot/bundle that is later unpacked and its `objects/pack/*.bitmap` files reused as-is rather than being regenerated). This reachability path could not be fully confirmed with the available context and would need further verification of how `ReplicateRepository`/import/fork RPCs handle `.bitmap` files.

### Recommendation
In `ReadEWAH`, validate that `bits` is consistent with `words` (e.g. `bits <= words*wordBits` and `bits > (words-1)*wordBits` per the EWAH encoding invariant), rejecting bitmaps where the declared bit count vastly exceeds what the supplied compressed words can represent. Additionally, consider capping `bits` (and the resulting `Unpack()` allocation) to a sane maximum relative to the repository's actual object count, since a bitmap's bit count should never meaningfully exceed the number of objects in the pack.

### Proof of Concept
1. Construct a minimal `.bitmap`-style EWAH-compressed bitmap header:
   - `uBits = 0x7FFFFFFF` (math.MaxInt32)
   - `uWords = 1` (minimal, so `rawSize = 1*8+4 = 12` bytes of trailing raw data, trivial to supply)
2. Feed this 8-byte header + 12 bytes of raw data into `ReadEWAH`, which succeeds since both fields individually pass the `math.MaxInt32` check and `raw` reads only 12 bytes.
3. Call `Unpack()` on the resulting `*Bitmap`: `nUnpackedWords = bits/64 + 1 ≈ 33,554,432`, so `buf := make([]byte, nUnpackedWords*8)` allocates ≈256 MiB from a ~20-byte crafted input.
4. Embedding four such bitmaps (Commits/Trees/Blobs/Tags) in one `.bitmap` file multiplies the effect to over 1 GiB per `LoadBitmap()` call, from a file only slightly larger than the reftable/bitmap headers themselves.

### Citations

**File:** internal/git/packfile/bitmap.go (L53-86)
```go
	ib := &IndexBitmap{}
	if err := ib.parseIndexBitmapHeader(r, idx); err != nil {
		return err
	}

	for _, ptr := range []**Bitmap{&ib.Commits, &ib.Trees, &ib.Blobs, &ib.Tags} {
		*ptr, err = ReadEWAH(r)
		if err != nil {
			return err
		}

		if err := (*ptr).Unpack(); err != nil {
			return err
		}
	}

	for i := range ib.bitmapCommits {
		header, err := readN(r, 6)
		if err != nil {
			return err
		}

		bc := &BitmapCommit{
			OID:       idx.Objects[binary.BigEndian.Uint32(header[:4])].OID,
			xorOffset: header[4],
			flags:     header[5],
		}

		if bc.Bitmap, err = ReadEWAH(r); err != nil {
			return err
		}

		ib.bitmapCommits[i] = bc
	}
```

**File:** internal/git/packfile/bitmap.go (L192-210)
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

	const ewahTrailerLen = 4
	rawSize := int64(e.words)*8 + ewahTrailerLen
	if rawSize > math.MaxInt32 {
		return nil, fmt.Errorf("bitmap does not fit in Go slice")
	}

	e.raw = make([]byte, int(rawSize))
```

**File:** internal/git/packfile/bitmap.go (L219-236)
```go
// Unpack expands e.raw, which is EWAH-compressed, into an uncompressed *big.Int.
func (e *Bitmap) Unpack() error {
	if e.bm != nil {
		return nil
	}

	const (
		wordSize = 8
		wordBits = 8 * wordSize
	)

	nUnpackedWords := e.bits / wordBits
	if e.bits%wordBits > 0 {
		nUnpackedWords++
	}

	buf := make([]byte, nUnpackedWords*wordSize)
	bufPos := len(buf)
```

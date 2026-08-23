### Title
Unbounded slice indexing in the reftable binary parser causes index-out-of-range panics — ([File: internal/git/reftable/reftable.go])

### Summary
Gitaly ships a custom, from-scratch parser for Git's `reftable` binary format in `internal/git/reftable/reftable.go`. Several functions compute byte offsets from length/size fields embedded in the reftable file and then slice the raw file buffer using those computed offsets *without ever validating them against the buffer's actual length*. This mirrors the reported Solidity bug class: an index/length value derived from external input is used to index into a fixed-size array/slice without a bounds check, causing an out-of-range access instead of a controlled/graceful error.

### Finding Description
`Table.extractBlockLen` reads a 3-byte block length directly from the buffer at a caller-supplied offset with no bounds check: [1](#0-0) 

`Table.parseRefBlock` uses that unchecked length to compute `blockStart+currentBS-2` and slices `src[blockStart+currentBS-2:]` (which can underflow if `currentBS < 2`, or overflow past `len(src)`), then derives `RestartStart` from it: [2](#0-1) 

`Table.getRefsFromBlock` repeatedly advances `idx` by `prefixLength`, `suffixLength`, and hash sizes read via `getVarInt`, then slices `src[idx:idx+suffixLength]`, `src[idx:idx+hashSize]`, and `refname[:prefixLength]` with no check that these stay within `len(src)` or `len(prefix)`: [3](#0-2) 

`getVarInt` only checks against `blockEnd`/`FullBlockSize`, not against the true length of `src`, so a crafted `blockEnd` or corrupted length field can still let `idx` walk past the end of the byte slice before the loop bound is checked: [4](#0-3) 

Unlike `internal/git/mvcc/manifest.go`, which validates every chunk offset/size against `dataEnd`/`len(m.Paths)` before slicing (e.g. `parseChunks` and `ParseManifest`), the reftable parser performs no equivalent defensive checks: [5](#0-4) 

Reftable files (`*.ref`) are read by this parser as part of the reftable-backend transaction/storage code path (`internal/gitaly/storage/storagemgr/partition/reftable.go`), which is exercised whenever a repository using the reftable reference backend is created, replicated, or snapshotted.

### Impact Explanation
A malformed or corrupted `.ref` table file — reachable via ordinary repository operations that write/replicate reftable files (repository creation, WAL-based replication/reconciliation across partitions, snapshot restore) — can drive `blockStart`, `currentBS`, `idx`, `prefixLength`, or `suffixLength` values that exceed the actual buffer length. Because none of the slicing operations validate against `len(src)`, this produces a Go runtime "index out of range" panic instead of a returned error. Since these parsing functions are not obviously wrapped in panic recovery at every call site, this can crash the goroutine/RPC handling the operation, resulting in denial of service for the Gitaly process handling that repository.

### Likelihood Explanation
Reftable files are ordinarily produced by trusted internal code, which somewhat limits attacker control over the byte layout. However, the parser is also exercised on data that arrives via replication/reconciliation and snapshot-restore paths in a multi-node deployment, where corruption, truncation, or a subtly malformed file (e.g., from a lagging/misbehaving replica or an interrupted write) can already break the invariants the parser silently assumes (`currentBS >= 2`, restart-count fields staying in range, embedded lengths never exceeding the file size). No authentication bypass or malicious-peer assumption is required for a truncated/corrupted table to reach the parser through normal repository lifecycle operations.

### Recommendation
Harden `internal/git/reftable/reftable.go` the same way `internal/git/mvcc/manifest.go` already does: before every slice operation derived from a length/offset field (`extractBlockLen`, `parseRefBlock`'s restart-count read, `getRefsFromBlock`'s `prefixLength`/`suffixLength`/hash-size advances, and `getVarInt`'s index advance), explicitly check the computed bounds against `len(src)` and return a descriptive parse error rather than allowing Go's runtime bounds check to panic. Add fuzz/unit tests that feed truncated and corrupted reftable files (short files, huge length fields, restart counts implying negative offsets) to `ParseTable`/`GetReferences` and assert a clean error is returned instead of a panic.

### Proof of Concept
1. Construct (or truncate) a `.ref` reftable file whose ref-block header encodes a `block_len` (`currentBS`) value that is either `0` or `1`, or that exceeds the actual remaining bytes in the file.
2. Feed this file through `reftable.ParseTable` followed by `Table.GetReferences()`.
3. In `parseRefBlock`, `blockStart+currentBS-2` underflows (if `currentBS < 2`, this wraps to a huge `uint`) or exceeds `len(src)`, and `bytes.NewBuffer(src[blockStart+currentBS-2:])` panics with "slice bounds out of range" instead of returning an error, crashing the calling goroutine/RPC handler. [6](#0-5)

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

**File:** internal/git/reftable/reftable.go (L203-219)
```go
// getVarInt parses a variable int and increases the index.
func (t *Table) getVarInt(src []byte, start uint, blockEnd uint) (uint, uint, error) {
	var val uint

	val = uint(src[start]) & 0x7f

	for (uint(src[start]) & 0x80) > 0 {
		start++
		if start > blockEnd {
			return 0, 0, fmt.Errorf("exceeded block length")
		}

		val = ((val + 1) << 7) | (uint(src[start]) & 0x7f)
	}

	return start + 1, val, nil
}
```

**File:** internal/git/reftable/reftable.go (L222-298)
```go
func (t *Table) getRefsFromBlock(src []byte, b *block) ([]git.Reference, error) {
	var references []git.Reference

	prefix := ""

	// Skip the block_type and block_len
	idx := b.BlockStart + 4

	for idx < b.RestartStart {
		var prefixLength, suffixLength, updateIndexDelta uint
		var err error

		idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
		if err != nil {
			return nil, fmt.Errorf("getting prefix length: %w", err)
		}

		idx, suffixLength, err = t.getVarInt(src, idx, b.RestartStart)
		if err != nil {
			return nil, fmt.Errorf("getting suffix length: %w", err)
		}

		extra := (suffixLength & 0x7)
		suffixLength >>= 3

		refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
		idx = idx + suffixLength

		idx, updateIndexDelta, err = t.getVarInt(src, idx, b.FullBlockSize)
		if err != nil {
			return nil, fmt.Errorf("getting update index delta: %w", err)
		}
		// we don't use this for now
		_ = updateIndexDelta

		reference := git.Reference{
			Name: git.ReferenceName(refname),
		}

		switch extra {
		case 0:
			// Deletion, no value
			reference.Target = t.shaFormat().ZeroOID.String()
		case 1:
			// Regular reference
			hashSize := t.shaFormat().Hash().Size()
			reference.Target = git.ObjectID(hex.EncodeToString(src[idx : idx+uint(hashSize)])).String()

			idx += uint(hashSize)
		case 2:
			// Peeled Tag
			hashSize := t.shaFormat().Hash().Size()
			reference.Target = git.ObjectID(hex.EncodeToString(src[idx : idx+uint(hashSize)])).String()

			idx += uint(hashSize)

			// For now we don't need the peeledOID, but we still need
			// to skip the index.
			// peeledOID := ObjectID(bytesToHex(t.src[idx : idx+uint(hashSize)]))
			idx += uint(hashSize)
		case 3:
			// Symref
			var size uint
			idx, size, err = t.getVarInt(src, idx, b.FullBlockSize)
			if err != nil {
				return nil, fmt.Errorf("getting symref size: %w", err)
			}

			reference.Target = git.ReferenceName(src[idx : idx+size]).String()
			reference.IsSymbolic = true
			idx = idx + size
		}

		prefix = refname

		references = append(references, reference)
	}
```

**File:** internal/git/reftable/reftable.go (L303-326)
```go
// parseRefBlock parses a block and if it is a ref block, provides
// all the reference updates.
func (t *Table) parseRefBlock(src []byte, headerOffset, blockStart, blockEnd uint) ([]git.Reference, error) {
	currentBS := t.extractBlockLen(src, blockStart+headerOffset)

	fullBlockSize := t.blockSize
	if fullBlockSize == 0 {
		fullBlockSize = currentBS
	} else if currentBS < fullBlockSize && currentBS < (blockEnd-blockStart) && src[blockStart+currentBS] != 0 {
		fullBlockSize = currentBS
	}

	b := &block{
		BlockStart:    blockStart + headerOffset,
		FullBlockSize: fullBlockSize,
	}

	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)

	return t.getRefsFromBlock(src, b)
```

**File:** internal/git/mvcc/manifest.go (L206-223)
```go
		if m.objStart+m.objCount > len(m.Paths) {
			return nil, fmt.Errorf("OBJS range [%d, %d) out of bounds (have %d paths)", m.objStart, m.objStart+m.objCount, len(m.Paths))
		}
	}

	// Process REFS chunk if present.
	refsChunk, err := chunks.AtIndex(chunks.refsIdx)
	if err == nil {
		if refsChunk.size%4 != 0 {
			return nil, fmt.Errorf("REFS chunk size %d not divisible by 4", refsChunk.size)
		}
		numRefs := refsChunk.size / 4
		m.refStackOrder = make([]string, numRefs)
		for i := range numRefs {
			idx := binary.BigEndian.Uint32(data[refsChunk.offset+i*4 : refsChunk.offset+i*4+4])
			if int(idx) >= len(m.Paths) {
				return nil, fmt.Errorf("REFS index %d out of bounds (have %d paths)", idx, len(m.Paths))
			}
```

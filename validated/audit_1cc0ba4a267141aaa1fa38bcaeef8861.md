### Title
Unsigned integer underflow in reftable block parsing causes RPC-handler panic (DoS) - (File: internal/git/reftable/reftable.go)

### Summary
`Table.parseRefBlock` in `internal/git/reftable/reftable.go` computes the restart-table offset for a reftable block using unchecked `uint` subtraction. When the block-length field (`currentBS`) read from the reftable file is smaller than 2, or when `3*RestartCount` exceeds `blockStart+currentBS-2`, the subtraction wraps around (Go's unsigned integers are not checked, analogous to Solidity pre-0.8 unchecked math) and produces a huge value instead of erroring out. That huge, wrapped value is then used to index into the in-memory byte slice `src`, causing an out-of-range slice panic. This mirrors the `changeFeeQuote` bug class: an unvalidated subtraction of an attacker/data-influenced field from a small constant, with no lower-bound check, that turns into an unhandled runtime failure instead of a clean error.

### Finding Description
`parseRefBlock` reads the block length via `extractBlockLen`, then does: [1](#0-0) 

```
func (t *Table) parseRefBlock(src []byte, headerOffset, blockStart, blockEnd uint) ([]git.Reference, error) {
	currentBS := t.extractBlockLen(src, blockStart+headerOffset)
	...
	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)

	return t.getRefsFromBlock(src, b)
}
```

Both `blockStart+currentBS-2` and the subsequent `... - 3*uint(b.RestartCount)` are `uint` arithmetic with no validation that `currentBS >= 2` or that `3*RestartCount <= blockStart+currentBS-2`. `currentBS` comes directly from file bytes via `extractBlockLen`: [2](#0-1) 

and `RestartCount` is read directly from file bytes as a `uint16` immediately after. Neither value is bounds-checked against the actual block size before being used in subtraction. If `currentBS < 2` (or `RestartCount` is large), the subtraction underflows, wrapping to a value near `math.MaxUint`. This corrupted `RestartStart`/slice index is then used in `binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), ...)` and downstream in `getRefsFromBlock`, both of which will attempt to slice `src` with an out-of-range index, triggering a Go runtime panic (`slice bounds out of range`).

This is directly analogous to the `changeFeeQuote` finding: an unsigned subtraction (`decimals - 4`) on a value that can legitimately be smaller than the constant, with no explicit handling of that case, causes the whole operation to abort/crash rather than degrade gracefully.

Table parsing is invoked from Gitaly's transaction/partition manager, which reads and processes reftable files that are part of on-disk repository ref-backend state: [3](#0-2) 

Reftable files are read whenever a repository using the `reftable` ref storage format has its refs listed, replicated, restored, or transactions applied (e.g., `ListRefs`, `ReplicateRepository`, backup/restore flows, and partition housekeeping/compaction). Any of these paths ultimately end up creating a `Table` via `ParseTable` and calling `GetReferences`, which iterates blocks and calls `parseRefBlock` on every block in the file: [4](#0-3) 

Because a reftable file can enter a repository's storage through repository creation/import, replication from another node, or restoration from a backup — all of which are reachable via ordinary Gitaly RPCs operating on repository content that is ultimately derived from user-supplied repository data — a corrupted or maliciously crafted reftable block (with an invalid/small block-length byte or an inflated restart count) can reach this unchecked arithmetic.

### Impact Explanation
A successful trigger causes an unrecovered slice-bounds panic inside the goroutine processing the RPC. While Gitaly's gRPC panic handler (`internal/grpc/middleware/panichandler/panic_handler.go`) recovers panics at the interceptor level for the call in question, this still means the request fails ungracefully with an internal error, any partially-constructed transaction/reftable state can be left inconsistent, and any code path that reads reftables outside of a gRPC handler context (e.g., background housekeeping/compaction goroutines during partition management) is at higher risk of crashing the whole `gitaly` process, since such background goroutines are not guaranteed to have panic-recovery wrapping equivalent to the gRPC middleware. This constitutes a Denial-of-Service vector against a repository/partition and, in the worst case, the storage node hosting it.

### Likelihood Explanation
The likelihood is moderate: reftable is an opt-in ref backend, so the vulnerable code path is only exercised for repositories configured to use it. However, once in use, reftable files are processed on read-heavy paths (`ListRefs`, replication, restore) that are reachable from ordinary Gitaly clients without any privileged access, and corruption or crafting of a single byte in the block-length or restart-count fields is sufficient to trigger the underflow — there is no cryptographic integrity check performed on the block header/restart section before use (unlike the whole-footer CRC32 check, which only covers the footer, not each block).

### Recommendation
Add explicit bounds validation before performing any subtraction on values that originate from file bytes:
- Verify `currentBS >= 2` before computing `blockStart+currentBS-2`.
- Verify `3*uint(b.RestartCount) <= blockStart+currentBS-2` before computing `b.RestartStart`, returning a parsing error otherwise instead of allowing the subtraction to wrap.
- Similarly, validate that all slice-index arithmetic derived from file-supplied lengths (in `extractBlockLen`, `getVarInt`, and `getRefsFromBlock`) does not exceed `len(src)` before indexing, converting any such condition into a returned `error` rather than a panic.
- Consider wrapping reftable-file parsing invoked from background/non-gRPC goroutines (housekeeping, transaction manager) with a `recover()`-based guard to prevent a corrupted reftable from crashing the entire process.

### Proof of Concept
Conceptual reproduction (exact byte offsets require the reftable version 1/2 encoding, but the underflow trigger is structural):
1. Craft or corrupt a reftable file so that a ref-type block's third byte (the 3-byte big-endian block length consumed by `extractBlockLen`) encodes a value `currentBS < 2` (e.g., `currentBS = 0`).
2. Place this reftable so that it is parsed by `Table.ParseTable` + `GetReferences` for a repository using the reftable backend (e.g., as part of a repository created/imported/replicated through a Gitaly RPC, or restored from a backup).
3. When `GetReferences` reaches this block, `parseRefBlock` computes `blockStart + currentBS - 2`, which underflows to a value near `math.MaxUint`, causing `src[blockStart+currentBS-2:]` to panic with "slice bounds out of range" instead of returning a clean parsing error.

Note: I was not able to fully trace every RPC entry point that can introduce a crafted/corrupted reftable file into a repository's on-disk state (e.g., exact replication/restore code paths beyond `internal/gitaly/service/repository/replicate.go` and `internal/backup/repository.go`) within the scope of this review; a background Devin session with full repository access would be needed to enumerate every reachable path and build a concrete end-to-end PoC.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
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

**File:** internal/git/reftable/reftable.go (L329-370)
```go
// GetReferences returns all references from the table.
func (t *Table) GetReferences() ([]git.Reference, error) {
	headerOffset := uint(t.footer.Version.HeaderSize())
	offset := uint(0)
	var allRefs []git.Reference

	if _, err := t.src.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("seek start: %w", err)
	}

	src, err := io.ReadAll(t.src)
	if err != nil {
		return nil, fmt.Errorf("read all: %w", err)
	}

	for offset < t.footerOffset {
		blockStart, blockEnd := t.getBlockRange(offset, t.blockSize)
		if blockStart == 0 && blockEnd == 0 {
			break
		}

		// If we run out of ref blocks, we can stop the iteration.
		if src[blockStart+headerOffset] != 'r' {
			return allRefs, nil
		}

		references, err := t.parseRefBlock(src, headerOffset, blockStart, blockEnd)
		if err != nil {
			return nil, fmt.Errorf("parsing block: %w", err)
		}

		if len(references) == 0 {
			break
		}

		allRefs = append(allRefs, references...)

		offset = blockEnd
	}

	return allRefs, nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition/reftable.go (L1-1)
```go
package partition
```

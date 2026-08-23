### Title
Unchecked unsigned-integer underflow in reftable ref-block parsing causes out-of-bounds panic (DoS) - ([File: internal/git/reftable/reftable.go])

### Summary
The Aloe-II `Oracle.observe` bug class — an unchecked arithmetic subtraction/multiplication on attacker-influenced values that silently wraps instead of being validated — has a direct analog in Gitaly's reftable parser. `Table.parseRefBlock` computes a block boundary (`RestartStart`) via unchecked Go `uint` subtraction using length fields taken directly from the file bytes, with no bounds validation before the subtraction. Because Go's built-in unsigned integer types wrap silently on underflow (functionally identical to Solidity's `unchecked` block), a crafted reftable block can force `RestartStart` to wrap to a huge value, which is then used to drive an unbounded read loop over the file's byte slice.

### Finding Description
`Table.parseRefBlock` reads the block length `currentBS` directly from file bytes via `extractBlockLen` and the 16-bit `RestartCount` from the tail of the block, then computes: [1](#0-0) 

```
b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

Neither `currentBS` nor `RestartCount` is validated against `blockStart` before this subtraction is performed. If `3*uint(RestartCount)` exceeds `blockStart + currentBS - 2`, the subtraction underflows. Go's `uint` (unsigned) arithmetic does not panic on overflow/underflow — it wraps, exactly the same failure mode the Sherlock report flags for Solidity's `unchecked` block missing a widening cast. This is the closest structural analog to the `Oracle.observe` bug: an in-language integer type performing silent wraparound arithmetic on externally-controlled operands, without the safety cast/validation that the reference implementation (Uniswap V3 for Solidity, or a bounds check here) would provide.

The resulting corrupted `RestartStart` is then used as the loop bound in `getRefsFromBlock`: [2](#0-1) 

```
for idx < b.RestartStart {
    ...
    refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
    idx = idx + suffixLength
    ...
}
```

With `RestartStart` wrapped to a near-`MaxUint`, the loop keeps advancing `idx` using more attacker-controlled varint lengths (`prefixLength`, `suffixLength`) read via `getVarInt`, which itself has no upper bound check relative to `len(src)` (only relative to `blockEnd`/`FullBlockSize`, themselves derived from the same corrupted-length data). Eventually `idx` (or `idx+suffixLength`) exceeds `len(src)`, causing a Go slice-index-out-of-range panic.

### Impact Explanation
`Table.GetReferences()`/`parseRefBlock` is invoked by the reftable-backend reference-reading path used during repository operations such as reference listing, replication, and WAL/transaction reference recording (`internal/gitaly/storage/storagemgr/partition/transaction_manager.go`, `transaction_manager_housekeeping.go`, `internal/backup/repository.go`). A crafted `.ref` table file — introduced via any path that lets an attacker place raw repository files (e.g., a repository snapshot/import/restore flow that writes files into a reftable-backed repository before Gitaly re-derives or reads references from it) — can trigger an unrecovered panic in the RPC-handling goroutine, crashing/aborting the request and potentially taking down the serving `gitaly` process for other in-flight requests sharing that goroutine group, i.e., a Denial of Service of the RPC handler.

### Likelihood Explanation
Likelihood is comparable to the original Medium-severity Aloe-II finding: it requires a specific, crafted combination of `currentBS`/`RestartCount` field values in a reftable block, which is only exercised by non-standard/malformed reftable files (Git itself would never emit a table with these underflowing fields, mirroring the "extremely low liquidity, days-long observation gap" precondition in the original report). It requires a path where such a malformed file lands in a repository directory before Gitaly parses it directly (without going through `git` to validate first). I could not fully confirm within the codebase which production RPC exposes writing arbitrary raw reftable bytes into a reftable-backed repository prior to `GetReferences`/`parseRefBlock` being invoked, so likelihood should be treated as uncertain pending confirmation of that RPC-level reachability.

### Recommendation
Add explicit bounds validation before performing the subtraction in `parseRefBlock`, mirroring the recommended Solidity fix pattern of validating/widening before arithmetic that can wrap:
```go
minRequired := uint(2) + 3*uint(b.RestartCount)
if currentBS < minRequired || blockStart+currentBS < minRequired {
    return nil, fmt.Errorf("invalid restart count %d for block length %d", b.RestartCount, currentBS)
}
b.RestartStart = blockStart + currentBS - minRequired
```
Additionally, `getVarInt` and `getRefsFromBlock` should validate that computed indices (`idx`, `idx+suffixLength`, `idx+hashSize`, etc.) never exceed `len(src)` before slicing, converting potential panics into handled parse errors.

### Proof of Concept
Not independently reproducible from the index alone — a full PoC requires constructing a raw reftable block with `currentBS` and a 16-bit `RestartCount` value chosen so that `3*RestartCount > blockStart+currentBS-2`, placing it as a `.ref` file that Gitaly parses via `Table.GetReferences`, and observing the resulting slice-bounds panic. Confirming the exact untrusted-write entry point (e.g., snapshot/import/restore RPC) that allows an attacker to place such a file ahead of parsing was not completed due to the iteration limit; this should be verified in a live session before treating this as fully confirmed rather than a code-pattern analog.

### Citations

**File:** internal/git/reftable/reftable.go (L222-248)
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

### Title
Unsigned integer underflow in reftable block parsing causes out-of-bounds panic (DoS) - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.parseRefBlock` and `Table.getRefsFromBlock` compute several offsets using Go's unsigned `uint` type without checking that subtrahends are smaller than minuends. A corrupted or maliciously crafted reftable block causes an unsigned-integer underflow analogous to the reported Solidity `int256(a-b)` bug class: instead of reverting/erroring cleanly, the subtraction wraps around to a value near `math.MaxUint`, which then drives subsequent slice-index arithmetic far outside the buffer, crashing the process with an out-of-range panic.

### Finding Description
`parseRefBlock` reads the block length (`currentBS`) directly from file bytes via `extractBlockLen` [1](#0-0) , then computes the restart-table offset:

```go
b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
``` [2](#0-1) 

`currentBS` and `RestartCount` are both parsed straight out of the file (`RestartCount` is read from `src[blockStart+currentBS-2:]` via `binary.Read`). If `currentBS < 2 + 3*RestartCount` — trivially achievable in a corrupted or attacker-supplied `.ref` block — the subtraction underflows because `blockStart`, `currentBS`, and `RestartCount` are all unsigned (`uint`/`uint16`). The result wraps to a huge value close to `math.MaxUint64` instead of erroring out.

That corrupted `RestartStart` is then used as a loop bound in `getRefsFromBlock`:
```go
for idx < b.RestartStart {
    ...
    idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
``` [3](#0-2) 

Because `RestartStart` is now astronomically large, the loop keeps running well past the end of `src`, and `getVarInt`/slice-index expressions such as `src[start]`, `src[idx:idx+suffixLength]` will index out of bounds, causing an unrecovered Go runtime panic (`index out of range`).

This is the direct Go analogue of the reported bug class: unsigned subtraction without first validating operand ordering, silently underflowing instead of failing safely — mirroring `int256(a-b)` where `a<b` in the original Solidity finding.

### Impact Explanation
An out-of-range panic in a goroutine handling a repository transaction (`Table.GetReferences`, called from `reftableRecorder.stageTables` in `internal/gitaly/storage/storagemgr/partition/reftable.go` during `git`-reftable-backend transactions) crashes the calling goroutine. Depending on recover-middleware, this can terminate an in-flight RPC and, in the worst case, disrupt the storage partition's transaction processing, denying service to legitimate users of the affected repository. `GetReferences`/`ParseTable` is only guarded by the reftable structural checksums in the footer (`parseFooter`), not by the per-block invariants exploited here.

### Likelihood Explanation
The corrupted-block condition requires a reftable file whose block-length field is inconsistent with its `RestartCount` field. Reftable files are normally produced by Gitaly's own `git` invocations, so this requires either (a) storage/import paths that copy raw `.ref` files from another repository (fork, replication, backup/restore) where the source repository's on-disk reftable data was corrupted or maliciously fabricated, or (b) any bit-level corruption of the file on disk. Because the CRC32 in the reftable footer covers only the footer, not per-block contents, a corrupted block is not otherwise detected until this parsing path runs, making the likelihood moderate for any deployment that ingests or replicates raw repository storage.

### Recommendation
Before performing the subtraction, explicitly validate that `currentBS >= 2 + 3*uint(b.RestartCount)` and that `RestartStart`, `idx`, and all subsequent computed offsets stay within `[blockStart, blockEnd)`/`len(src)`, returning an error instead of continuing. Apply the same bounds validation to all offset arithmetic in `getRefsFromBlock` and `getVarInt` (e.g., checking `idx+suffixLength <= blockEnd` before slicing) so that malformed blocks fail with a parse error rather than underflowing and panicking.

### Proof of Concept
1. Craft (or corrupt) a reftable `.ref` file whose ref-block header encodes a `block_len` (`currentBS`) that is smaller than `2 + 3*restart_count` (e.g., set `restart_count` high while keeping the block length minimal), while keeping the footer CRC32 valid (footer checksum does not cover block-level contents).
2. Place this file into a repository's `reftable/` directory reachable by Gitaly (e.g., via a fork/replication path that copies raw reftable files, or by directly placing it in a snapshot used by a transaction).
3. Trigger a code path that calls `reftable.ParseTable` followed by `Table.GetReferences()` (e.g., `reftableRecorder.stageTables` during a reference-update transaction, `internal/gitaly/storage/storagemgr/partition/reftable.go:94`).
4. `parseRefBlock` computes `RestartStart = blockStart + currentBS - 2 - 3*uint(RestartCount)`, which underflows to a near-max `uint` value; the subsequent loop in `getRefsFromBlock` indexes far past `len(src)`, causing a runtime panic.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

**File:** internal/git/reftable/reftable.go (L228-248)
```go
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

**File:** internal/git/reftable/reftable.go (L320-324)
```go
	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

### Title
Unsigned integer underflow in reftable ref-block restart-offset computation leads to out-of-bounds panic - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.parseRefBlock` computes the start of a reftable block's restart table using peer/repository-controlled length fields without validating that the subtraction cannot underflow, mirroring the reported Sonic bug class where an attacker-controlled length is subtracted from another value without a bounds check, producing a garbage (wrapped) value that is used unguarded afterward.

### Finding Description
`parseRefBlock` reads the block length `currentBS` directly from file bytes via `extractBlockLen` [1](#0-0) , then computes the restart-table start with:
```go
b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
``` [2](#0-1) 
Both `currentBS` (the block length) and `b.RestartCount` (a `uint16` read from `src[blockStart+currentBS-2:]`) are values taken straight from the reftable file contents, with no check that `currentBS >= 2` or that `3*RestartCount <= currentBS-2`. Because `blockStart`, `currentBS`, and `RestartCount` are all unsigned (`uint`/`uint16`), a small `currentBS` or an inflated `RestartCount` causes the subtraction to wrap around to a very large `uint` value instead of erroring out — the same underflow pattern flagged in the Sonic advisory (subtracting a peer-controlled diff from a value without verifying the diff doesn't exceed it, producing silent wraparound instead of a rejected/malformed error).

The wrapped `RestartStart` is then used as the loop bound in `getRefsFromBlock`:
```go
for idx < b.RestartStart {
    ...
    idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
    ...
    refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
``` [3](#0-2) 
Since `idx < b.RestartStart` will essentially always be true for a huge, underflowed `RestartStart`, and `getVarInt`'s own bound check compares `start > blockEnd` where `blockEnd` is the same corrupted value, the loop keeps advancing `idx` and slicing `src` (`prefix[:prefixLength]`, `src[idx:idx+suffixLength]`) far past the actual buffer, which will panic with an out-of-range slice index rather than being rejected as malformed input.

### Impact Explanation
A crafted reftable block (block length / restart-count fields) causes an unbounded/garbage restart offset that is used to drive further byte-slice indexing without re-validation, resulting in a runtime panic when parsing the ref block. Since `GetReferences`/`ParseTable` are invoked when reading reftable-backed repositories (e.g., during transaction processing in `internal/gitaly/storage/storagemgr/partition/transaction_manager.go` and `partition/reftable.go`), a malformed reftable reaching this code path can crash the goroutine/handler processing it, denying service for that repository's transaction/read path — analogous to the Sonic bug's DoS via garbage data that "stalls indefinitely" rather than being cleanly rejected.

### Likelihood Explanation
Likelihood is moderate to low-confidence without full trace of every producer of reftable files reachable from ordinary user actions (e.g., replicated/imported repositories, snapshots, or WAL-recovered data reaching `ParseTable`). The parsing code itself accepts attacker-shaped byte layout with no defensive bound checks on the subtraction, which is the concrete, provable root cause; whether an ordinary unprivileged user can supply the raw bytes of a reftable to this exact function (as opposed to only Gitaly-generated reftables) could not be fully confirmed from the available index — `internal/gitaly/service/repository/replicate.go` and `list_refs_test.go` did not show a direct reftable byte-content injection path in the excerpts retrieved.

### Recommendation
In `parseRefBlock`, validate `currentBS >= 2` and `3*uint(b.RestartCount) <= currentBS-2` (and that the resulting `RestartStart` lies within `[blockStart, blockEnd]`) before computing `b.RestartStart`; return a malformed/parse error instead of allowing the subtraction to wrap. Similarly harden `getRefsFromBlock`'s per-field length checks (`prefixLength <= len(prefix)`, `idx+suffixLength <= blockEnd`) before slicing.

### Proof of Concept
Construct a reftable ref block where the block-length byte sequence yields `currentBS < 2` (or a `RestartCount` such that `3*RestartCount > currentBS-2`), e.g., patch the block header bytes at the block's start so `extractBlockLen` returns 0 or 1. Feed this file to `reftable.ParseTable` followed by `Table.GetReferences()`; the subtraction `currentBS - 2 - 3*RestartCount` underflows to a huge `uint`, and the subsequent loop in `getRefsFromBlock` will index `src` out of bounds, panicking the process. Full confirmation of an external-facing trigger path (which RPC/import surface writes attacker-influenced reftable bytes that are later parsed by this function) requires deeper tracing than the available index coverage allowed — this should be validated with a live Devin session against the complete repository.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

**File:** internal/git/reftable/reftable.go (L230-248)
```go
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

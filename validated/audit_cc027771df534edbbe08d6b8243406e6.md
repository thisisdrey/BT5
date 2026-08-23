### Title
Unsigned integer underflow in reftable ref-block parsing causes out-of-bounds panic on crafted reftable data - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.parseRefBlock` computes the start of a reftable block's restart array using unchecked unsigned subtraction/multiplication over attacker-influenced values read directly from a reftable file. When the computed value underflows, it wraps to a huge `uint`, which is then used as a loop bound and byte-slice index in `getRefsFromBlock`, leading to an out-of-bounds panic (analogous to the "phantom overflow" bug class: an intermediate arithmetic value wraps/overflows before use, producing an unexpectedly large or small result that corrupts downstream logic).

### Finding Description
`parseRefBlock` reads a 16-bit `RestartCount` directly from file bytes and then computes: [1](#0-0) 

```go
if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
    return nil, fmt.Errorf("reading restart count: %w", err)
}
b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

`currentBS` (`extractBlockLen`) and `RestartCount` are both taken from raw file bytes without any bound validation relative to each other before this subtraction. Because `uint` is unsigned, if `3*uint(b.RestartCount)` exceeds `blockStart + currentBS - 2`, the subtraction underflows and wraps around to a value near `math.MaxUint64` rather than erroring out — the same root cause pattern as the reported phantom-overflow bug class (an unchecked arithmetic operation on attacker-controlled magnitudes produces a value inconsistent with its intended range).

That corrupted `RestartStart` is then used unchecked as a loop bound in `getRefsFromBlock`: [2](#0-1) 

```go
idx := b.BlockStart + 4
for idx < b.RestartStart {
    ...
    idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
    ...
    refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
    idx = idx + suffixLength
    ...
}
```

With `b.RestartStart` wrapped to a near-maximum value, the loop condition `idx < b.RestartStart` remains true far beyond the actual buffer bounds. `getVarInt` also uses `blockEnd` (here `b.RestartStart`) as its own bound check (`if start > blockEnd`), so the corrupted bound defeats that guard as well: [3](#0-2) 

Reads such as `src[idx:idx+suffixLength]` and `src[idx : idx+uint(hashSize)]` then run past the end of the in-memory `src` slice (populated from the whole file via `io.ReadAll` in `GetReferences`), triggering a Go runtime panic (slice bounds out of range) rather than a handled error.

### Impact Explanation
This is reachable via any code path that parses a reftable file with `reftable.ParseTable`/`GetReferences`, which is used when Gitaly reports reference information (`ReferencesInfoForRepository`) and during repository replication/reference-backend detection for repositories using the `reftable` reference backend. A crafted or corrupted reftable file (e.g., arriving via repository replication from another node, or a repository whose reftable data has been tampered with/corrupted) can trigger a runtime panic in the Gitaly process handling the RPC, causing denial of service for that request/goroutine. Because there is no bounds check between `RestartCount`, `currentBS`, and the actual block size before the subtraction, the parser cannot reject malformed input gracefully and instead crashes on out-of-bounds slice access.

### Likelihood Explanation
Reaching this requires a reftable-backed repository with a corrupted/malicious `.ref` table file — the file format is git-internal, but Gitaly (and replicated peers) trust the byte layout without validating that `3*RestartCount` fits inside the parsed block size before subtracting. This makes the likelihood moderate: it depends on an attacker being able to influence a repository's on-disk reftable content (e.g., through repository replication from a compromised/faulty source, or repository corruption/tampering scenarios), rather than a purely remote unauthenticated RPC field.

### Recommendation
Validate that `3*uint(b.RestartCount) <= blockStart+currentBS-2` (and more generally that `currentBS` is at least large enough to contain the restart-count field and restart array) before performing the subtraction; return a parse error instead of continuing with a wrapped value. Additionally, bound-check `idx`, `idx+suffixLength`, `idx+size`, and `idx+hashSize` against `len(src)` (not just against the derived `RestartStart`/`FullBlockSize`) before slicing, so that any residual inconsistency in the derived offsets results in a clean parse error rather than a panic.

### Proof of Concept
1. Construct or corrupt a valid reftable `.ref` file for a repository using the `reftable` reference backend (`extensions.refstorage = reftable`).
2. In a ref block, set the trailing 2-byte `RestartCount` field to a value such that `3 * RestartCount > (block_length - 2)` for that block (i.e., larger than the actual restart-array region), while keeping the header/footer CRC checks satisfied for that record (footer CRC covers the footer only, not per-block content, so per-block content is unauthenticated).
3. Call `reftable.ParseTable(path)` followed by `table.GetReferences()` (reached in production via `stats.ReferencesInfoForRepository` for `RepositoryInfo` RPC calls, or via replication/reference-backend detection code paths that read reftable files).
4. `parseRefBlock` computes `b.RestartStart = blockStart + currentBS - 2 - 3*uint(RestartCount)`, which underflows to a near-`MaxUint64` value.
5. `getRefsFromBlock`'s loop `for idx < b.RestartStart` continues reading past the end of the `src` byte slice, causing a Go runtime "slice bounds out of range" panic when Gitaly attempts `src[idx:idx+suffixLength]` or similar slicing operations.

### Citations

**File:** internal/git/reftable/reftable.go (L204-219)
```go
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

### Title
Reftable ref-block decoder truncates/misreads varint-encoded lengths without bounds validation, causing out-of-bounds slicing and incorrect reference-name/target decode - (File: internal/git/reftable/reftable.go)

### Summary
The Kakarot report's root cause is a decode routine that reads only part of an encoded value (decoding a `(uint128, uint128)` pair as a plain `uint256`), silently producing a wrong result that is then trusted by callers. Gitaly's reftable reader has an analogous class of bug: `getVarInt`, `getRefsFromBlock`, and `parseRefBlock` decode length/count fields from an untrusted `.ref` table body and immediately use the decoded values to slice the buffer without validating them against the buffer bounds, the enclosing block, or the table size.

### Finding Description
`getVarInt` in `internal/git/reftable/reftable.go` decodes a variable-length integer from `src` and returns the parsed value plus the next index, checking only that the *index* does not exceed `blockEnd` — it never validates the resulting *value* itself. [1](#0-0) 

`getRefsFromBlock` then uses these decoded values directly to slice the buffer with no bounds check against `len(src)` or the current block/restart boundaries:
```go
idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
...
idx, suffixLength, err = t.getVarInt(src, idx, b.RestartStart)
...
extra := (suffixLength & 0x7)
suffixLength >>= 3
refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
idx = idx + suffixLength
``` [2](#0-1) 

`prefixLength` is used to slice the previous `prefix` string (`prefix[:prefixLength]`) without checking that `prefixLength <= len(prefix)`, and `suffixLength` (after being right-shifted by 3 to strip the packed "extra" bits) is used to slice `src[idx:idx+suffixLength]` without checking that `idx+suffixLength <= len(src)` or `<= b.RestartStart`. Similarly, the symref-length branch reads a length via `getVarInt` and slices `src[idx : idx+size]` with the same lack of validation: [3](#0-2) 

`parseRefBlock` also computes `RestartStart` from an attacker-influenced `RestartCount` field read straight off the wire via `binary.Read`, without validating that the resulting `RestartStart` stays within the block: [4](#0-3) 

Because none of `prefixLength`, `suffixLength`, `size`, or `RestartCount` are range-checked before being used as slice bounds, a malformed or crafted reftable body can drive these computations to negative/overflowed offsets or lengths that exceed the buffer, which in Go causes an out-of-bounds slice-bounds-out-of-range panic, or — when the wrap-around still yields in-bounds-but-wrong offsets — a mis-decoded reference name/target being silently accepted, mirroring the "erroneous decode causes wrong-but-accepted value" failure mode in the original report.

### Impact Explanation
`Table.GetReferences()` is the mechanism Gitaly uses to read committed reftable stacks in its reftable-backed reference storage. A crafted or corrupted `.ref` file — reachable through paths that write/import reftable content, or through races/corruption of tables that Gitaly reads without re-validating a checksum per-block (only header/footer are checksummed, not the block payload) — can trigger a panic in the RPC handling process (denial of service for that request/goroutine) or, more subtly, cause a reference name/target to be resolved from the wrong byte range, i.e., reads referencing the wrong OID/refname. Given references drive access-control-adjacent decisions (e.g., which OID a ref points to), a silently wrong decode has correctness/security implications for any caller trusting `GetReferences()`'s output.

### Likelihood Explanation
The block payload is not covered by the reftable's CRC32 (only the header and footer are checksummed), so any bit corruption or a deliberately crafted reftable body (e.g., introduced during import/migration flows that materialize reftable files or through any path that can influence the raw bytes making it onto disk before `GetReferences()` reads them) reaches this decode path without further defense. The lack of bounds checks on `prefixLength`, `suffixLength`, `size`, and `RestartCount` makes exploitation straightforward once such a file is read.

### Recommendation
Validate every decoded length/count against the actual remaining buffer size before using it to slice or index:
- In `getVarInt`, reject values whose accumulated shifts would overflow reasonable bounds.
- In `getRefsFromBlock`, verify `prefixLength <= len(prefix)`, `idx+suffixLength <= b.RestartStart` (and `<= len(src)`), and the same for the symref `size`, returning an error instead of panicking or silently mis-slicing.
- In `parseRefBlock`, validate that the computed `RestartStart` lies within `[blockStart, blockStart+currentBS)` before using it.

### Proof of Concept
Construct a reftable file whose ref block encodes a `suffix_length` varint such that, after `suffixLength >>= 3`, `idx+suffixLength` exceeds `len(src)` (or, alternatively, encodes `RestartCount` such that `b.RestartStart` computed in `parseRefBlock` underflows/overflows `uint`). Feed this file path into `ParseTable` followed by `GetReferences()`; the resulting `src[idx:idx+suffixLength]` slice expression panics with "slice bounds out of range" (confirmed by inspecting the code paths in `internal/git/reftable/reftable.go` lines 199-326 — no dynamic testing was performed, so exact panic behavior under all inputs is not independently verified against a live binary).

### Citations

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

**File:** internal/git/reftable/reftable.go (L282-292)
```go
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
```

**File:** internal/git/reftable/reftable.go (L320-324)
```go
	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

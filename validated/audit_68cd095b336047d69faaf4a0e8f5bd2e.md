### Title
Unvalidated length fields from reftable data used as slice bounds cause a Gitaly process panic - ([File: internal/git/reftable/reftable.go])

### Summary
`getRefsFromBlock` in `internal/git/reftable/reftable.go` parses variable-length integers directly out of a reftable ref block and immediately uses them as slice indices/lengths without validating them against the actual buffer size, mirroring the root cause of the Zebra advisory (an unvalidated, attacker-influenced length/offset used directly in byte-level string/slice indexing, causing the process to panic).

### Finding Description
`getRefsFromBlock` reads `prefixLength` and `suffixLength` via `getVarInt` (which only validates that the *scan cursor* stays within `blockEnd`, not that the resulting length values are sane) and then does: [1](#0-0) 

```go
extra := (suffixLength & 0x7)
suffixLength >>= 3

refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
idx = idx + suffixLength
```

Neither `prefixLength` (used to slice the previous `prefix` string) nor `suffixLength` (used to slice `src`) is checked against the length of `prefix` or the remaining bytes in `src`/`blockEnd`. `getVarInt` itself only guards the varint continuation bytes against `blockEnd`, not the *decoded value*: [2](#0-1) 

Similarly, `extractBlockLen` and `parseRefBlock` read from `src` at computed offsets (`blockStart+1`, `blockStart+currentBS-2`, etc.) without confirming those offsets are within the slice bounds: [3](#0-2) [4](#0-3) 

Any corrupted or maliciously crafted reftable block (`prefixLength` larger than `prefix`, or `suffixLength`/hash-size skips that run past `blockEnd`/`len(src)`) will trigger a Go "slice bounds out of range" panic inside `GetReferences`/`ParseTable`. This is directly analogous to the Zebra bug class: an untrusted, attacker-supplied length field is trusted for direct byte-index slicing instead of being range-checked, and the resulting panic is fatal to the calling process rather than a recoverable error.

### Impact Explanation
`Table.GetReferences()` is used to read back reftable data written during transaction commit/resequencing (`internal/gitaly/storage/storagemgr/partition/reftable.go`), which runs inside a live Gitaly transaction path. If the tables it reads are truncated, corrupted, or crafted (e.g., through a repository import/replication scenario or a torn/partial write that the code doesn't fully guard against), the resulting unrecovered panic can crash the Gitaly process, denying service to all repositories on that node — the same "single crafted input aborts the whole process" impact profile described in the report. Because none of the callers wrap `GetReferences`/`ParseTable` invocations with panic recovery, a single malformed reftable file is sufficient to take the process down.

### Likelihood Explanation
Reftable files are normally produced by Git itself, which constrains the realistic likelihood of naturally reaching this state through totally organic use. However, the parsing routine performs no defensive bounds-checking of decoded values at all, so any code path that feeds Gitaly a reftable file it did not fully trust (e.g., truncated/partially-written tables during replication, imported repositories, or race conditions during compaction/resequencing) can hit this un-validated slice arithmetic. The complete absence of length validation despite parsing untrusted byte streams is a real, concrete weakness regardless of exact trigger frequency.

### Recommendation
Add explicit bounds checks before every slice operation derived from `getVarInt`-decoded values in `getRefsFromBlock`, `extractBlockLen`, and `parseRefBlock`:
- Verify `prefixLength <= len(prefix)` before slicing `prefix[:prefixLength]`.
- Verify `idx+suffixLength <= blockEnd` (or `len(src)`) before slicing `src[idx:idx+suffixLength]`.
- Verify `idx+hashSize <= blockEnd` before extracting object IDs.
- Verify `blockStart+4 <= len(src)` in `extractBlockLen` and that `blockStart+currentBS-2 >= blockStart` in `parseRefBlock`.
Return a descriptive parse error instead of allowing Go's slice-bounds panic to propagate, and add a `recover()` guard around reftable parsing entry points as defense-in-depth so any residual arithmetic error becomes a handled error rather than a process-crashing panic.

### Proof of Concept
Construct (or corrupt) a reftable ref block such that the varint-encoded `suffixLength` for a ref entry, once added to the current cursor `idx`, exceeds `len(src)` (or `blockEnd`) — for example, truncate a valid reftable file a few bytes into a ref block's entry data, or hand-craft a REFT-format file where a ref record advertises a suffix length larger than the remaining block bytes. Calling `Table.GetReferences()` on this file (as invoked from `internal/gitaly/storage/storagemgr/partition/reftable.go`'s table-parsing flow) will panic with "slice bounds out of range" in `getRefsFromBlock`'s `src[idx:idx+suffixLength]` expression, crashing the process since no recovery mechanism exists around this code path.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

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

**File:** internal/git/reftable/reftable.go (L244-248)
```go
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

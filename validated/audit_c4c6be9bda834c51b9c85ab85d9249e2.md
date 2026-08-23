### Title
Out-of-bounds slice access / integer underflow in reftable block parsing causes DoS - (File: internal/git/reftable/reftable.go)

### Summary
`internal/git/reftable/reftable.go` parses Git's binary reftable (`.ref`) format with unchecked arithmetic on length and count fields taken directly from file bytes. `parseRefBlock` and `getRefsFromBlock` compute slice offsets from these untrusted values without validating them against the actual buffer length, mirroring the class of bug fixed upstream in Nethermind's `NibbleExtensions` (unchecked offset/length arithmetic on attacker-influenced binary data leading to out-of-bounds memory access).

### Finding Description
`parseRefBlock` reads a 16-bit `RestartCount` from the block and computes: [1](#0-0) 

`b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)` is unsigned arithmetic with no check that `3*RestartCount <= currentBS-2`. A crafted `RestartCount` large relative to `currentBS` underflows `uint`, producing a huge `RestartStart` value.

`getRefsFromBlock` then loops `for idx < b.RestartStart`, reading var-ints and length-prefixed strings out of `src` using attacker-controlled `prefixLength`, `suffixLength`, and hash/symref sizes with no bounds checks against `len(src)`: [2](#0-1) 

`extractBlockLen` and `getVarInt` similarly index into `src` using offsets derived from file content without verifying they are within `len(src)`: [3](#0-2) 

Any of `record[:end]` style slicing (`prefix[:prefixLength]`, `src[idx:idx+suffixLength]`, `src[idx:idx+hashSize]`) can panic with "slice bounds out of range" when the corrupted/crafted length fields exceed actual buffer bounds — this is a Go-level analog of an OOB memory read.

### Impact Explanation
A malformed `.ref` table file (via a corrupted/malicious push that gets written to reftable storage, a replicated repository from a compromised/malicious peer Gitaly node, or a restored backup/imported repository) causes `Table.GetReferences()` to panic. This function is invoked from production, non-test code paths including `internal/backup/repository.go` and `internal/gitaly/service/repository/replicate.go`, as well as internally during transaction reference resequencing (`internal/gitaly/storage/storagemgr/partition/reftable.go`, `transaction_manager_housekeeping.go`). A panic in these RPC-triggered code paths crashes the goroutine/handler processing the RPC, resulting in Denial of Service for that repository's operations (backup, replication, or reference commit) — reachable without needing a privileged actor, purely by supplying/replicating a corrupted-but-reachable reftable payload.

### Likelihood Explanation
Reftable-format storage is the default reference backend path when the `reftable`/MVCC backend is enabled, and Gitaly writes/rewrites `.ref` files during ordinary reference-update transactions and background pack-refs. Because none of the length/count fields extracted from these binary files are validated against the buffer size before being used in slicing arithmetic, any code path that reads a reftable produced by an untrusted or corrupted source (replicated repository, restored backup, or a table corrupted mid-write) can trigger the panic. This raises likelihood above "low" for Gitaly deployments using the reftable backend, though it requires the reftable content itself to be attacker-influenced (e.g. through replication from a malicious/compromised peer or import of a crafted repository) rather than a purely local user-supplied RPC field.

### Recommendation
Add explicit bounds validation before every arithmetic/slicing operation in `internal/git/reftable/reftable.go`:
- In `parseRefBlock`, verify `3*uint(b.RestartCount) <= currentBS-2` before computing `RestartStart`, and verify `RestartStart` and `currentBS` stay within `[blockStart, blockEnd]`.
- In `getRefsFromBlock`, validate `idx+suffixLength`, `idx+hashSize`, and `prefixLength <= len(prefix)` against `len(src)`/`b.RestartStart`/`b.FullBlockSize` before slicing, returning a parse error instead of panicking.
- In `extractBlockLen` and `getVarInt`, check `blockStart+4 <= len(src)` and `start <= len(src)` before indexing.
- Add fuzz/unit tests with truncated and adversarial reftable blocks (oversized restart counts, oversized suffix/prefix lengths) to ensure parse errors are returned instead of panics.

### Proof of Concept
Construct a minimal valid reftable header/footer wrapping a single "r" block whose trailing 2-byte restart count is set to a value such that `3*RestartCount > currentBS-2` (e.g., `RestartCount = 0xFFFF` in a small block). Feed this file path to `reftable.ParseTable` followed by `Table.GetReferences()` (as done in `internal/backup/repository.go` or `internal/gitaly/service/repository/replicate.go`); the unsigned underflow in `RestartStart` combined with unchecked slicing in `getRefsFromBlock`/`getVarInt` causes a runtime "slice bounds out of range" panic instead of a graceful parse error, crashing the calling handler/goroutine.

### Citations

**File:** internal/git/reftable/reftable.go (L198-219)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}

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

**File:** internal/git/reftable/reftable.go (L230-292)
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
```

**File:** internal/git/reftable/reftable.go (L320-324)
```go
	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

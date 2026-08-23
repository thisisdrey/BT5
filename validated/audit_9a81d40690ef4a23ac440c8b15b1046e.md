Based on my investigation, I found a strong structural analog: `internal/git/reftable/reftable.go` parses reftable binary block data using unchecked slice indexing derived from length fields embedded in the file, exactly mirroring the "index/array-bounds not validated before use" defect class from the Angle report (`LibHelpers.piecewiseLinear` indexing into `xArray`/`yArray` without bounds checks).

### Title
Reftable block parser panics (DoS) on out-of-range length fields without bounds validation - (File: internal/git/reftable/reftable.go)

### Summary
`Table.getRefsFromBlock` and its helper `getVarInt` decode `prefixLength`, `suffixLength`, and hash/symref sizes directly from the reftable block bytes and use them immediately to slice `src` and the running `prefix` string, without validating that the decoded lengths are within the bounds of the underlying buffer, analogous to how `LibHelpers.piecewiseLinear` indexed into `xArray`/`yArray` using an unvalidated index.

### Finding Description
In `getRefsFromBlock`, once `prefixLength` and `suffixLength` are decoded via `getVarInt`, they are used directly: [1](#0-0) 
`prefix[:prefixLength]` will panic with "slice bounds out of range" if `prefixLength > len(prefix)`, and `src[idx:idx+suffixLength]` will panic if `idx+suffixLength` exceeds `len(src)`. Similarly, the hash-target and symref branches slice `src` using `hashSize`/`size` without checking that `idx+size` stays within `b.FullBlockSize` or `len(src)`: [2](#0-1) 
`getVarInt` itself only validates that intermediate continuation bytes stay within `blockEnd`, but the final decoded `val` is unbounded and not checked against the remaining block/file size: [3](#0-2) 
This mirrors the Angle bug class: a value decoded from data is used as an index/length without validating it stays inside the array/buffer that will be indexed with it.

### Impact Explanation
A crafted or corrupted reftable block (large `prefixLength`, `suffixLength`, or symref `size` relative to the actual buffer/prefix length) causes an unrecovered Go slice-bounds panic. `Table.GetReferences` is called from the storage/partition transaction path when resequencing reftables written during a write transaction (`internal/gitaly/storage/storagemgr/partition/reftable.go`, via `reftable.ParseTable` inside `stageTables`), from backup/restore code (`internal/backup/repository.go`), and from replication (`internal/gitaly/service/repository/replicate.go`). While gRPC panic recovery middleware (`internal/grpc/middleware/panichandler/panic_handler.go`) will catch panics in RPC handler goroutines and convert them to `Internal` errors, a panic inside code paths invoked from background/transaction-manager goroutines (e.g. WAL application during `stageTables`) may not be wrapped by the same recovery boundary, which can crash the storage/partition worker and disrupt the entire partition (a broader denial of service than a single RPC failure).

### Likelihood Explanation
Reftable files are normally produced by trusted Git/Gitaly tooling (`git pack-refs`, `git update-ref` with reftable backend), so triggering malformed length fields requires either disk corruption, a bug in the reftable-writing path, or a code path that consumes attacker-influenceable reftable bytes (e.g., a restored backup or replicated repository whose reftable files were written elsewhere). Because Gitaly does not independently re-validate `prefixLength`/`suffixLength`/`size` against buffer bounds before parsing, likelihood of the crash firing on any corrupted or unexpected reftable content is high once such a file reaches `GetReferences`; I could not fully confirm from the index whether any external/less-trusted input (e.g., attacker-controlled bundle content restored as a reftable-backed repository) can reach this parser, so likelihood as an externally triggerable vulnerability (vs. an internal robustness gap) is uncertain.

### Recommendation
Mirror the Angle mitigation pattern (explicit bounds check before indexing): in `getVarInt`, validate that the returned `val` does not cause subsequent slicing to exceed the block/file bounds. In `getRefsFromBlock`, before each slicing operation, add explicit checks such as `if prefixLength > uint(len(prefix)) { return nil, fmt.Errorf(...) }` and `if idx+suffixLength > b.RestartStart /* or len(src) */ { return nil, fmt.Errorf(...) }`, doing the same for the hash-target and symref-size slices, returning a structured parse error instead of allowing a panic to propagate.

### Proof of Concept
Not independently reproduced (no execution environment available); the vulnerable code paths and lack of bounds checks are demonstrated via the cited line ranges. A concrete PoC would require constructing a `.ref` reftable file with a ref block whose varint-encoded `suffixLength` (or `prefixLength`) exceeds the remaining bytes in the block/buffer, then invoking `Table.GetReferences()` on it (e.g., via `reftable.ParseTable(path)` as called from `internal/gitaly/storage/storagemgr/partition/reftable.go` `stageTables`) to trigger a `slice bounds out of range` panic.

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

**File:** internal/git/reftable/reftable.go (L244-248)
```go
		extra := (suffixLength & 0x7)
		suffixLength >>= 3

		refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
		idx = idx + suffixLength
```

**File:** internal/git/reftable/reftable.go (L265-293)
```go
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
```

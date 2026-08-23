### Title
Unbounded slice indexing while parsing reftable ref blocks causes RPC-handler panic/DoS - (File: `internal/git/reftable/reftable.go`)

### Summary
`Table.getRefsFromBlock` and its helper `Table.getVarInt` decode a git reftable's variable-length-encoded fields (prefix length, suffix length, hash size, symref size) and immediately use those attacker/writer-influenced lengths to slice the in-memory buffer, without verifying the resulting offsets stay inside the buffer. A single malformed or truncated block causes a Go slice-bounds-out-of-range panic that aborts parsing of the *entire* table/file — not just the offending ref — exactly mirroring the reported bug class where one bad entry (`feeReceiver`/ref-block) breaks the whole batch operation (`distribute`/`GetReferences`) for everyone else in it.

### Finding Description
`getVarInt` only bounds-checks the *scan* of the continuation bits against `blockEnd`/`FullBlockSize`, but never validates that the decoded `val` (used as `prefixLength`, `suffixLength`, hash size or symref size) plus the current cursor stays within `len(src)`: [1](#0-0) 

Those decoded lengths are then used directly for slicing without any additional length checks: [2](#0-1) [3](#0-2) 

For example, `refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])` and `git.ReferenceName(src[idx : idx+size]).String()` for symrefs will panic with "slice bounds out of range" if `prefixLength`, `suffixLength`, or `size` exceed the remaining buffer — this is never checked against `b.RestartStart`/`b.FullBlockSize`/`len(src)` before use, unlike the `getVarInt` scanning loop which does check its own cursor. This is structurally the same class of defect as the reported issue: a decode step trusts an untrusted length/return value without validating it before it is consumed, and one bad record aborts the whole shared operation (`GetReferences`, which returns *all* references in the table) instead of failing gracefully for just the bad record.

`Table.parseRefBlock` also computes `blockStart+currentBS-2` and `b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)` from header fields read out of the file with no cross-validation against block size, which can similarly underflow/overflow into out-of-bounds reads: [4](#0-3) 

The reftable format is used by Gitaly's reftable ref backend and is exercised through Gitaly's own storage/partition layer, e.g. `internal/gitaly/storage/storagemgr/partition/reftable.go` and the transaction manager, which read back reftable files written as part of ordinary reference-update transactions (i.e., ordinary pushes / ref updates flow through this code as part of normal repository operation).

### Impact Explanation
A single corrupted or adversarially-crafted reftable block causes a Go runtime panic during `GetReferences()`, which is caught by Gitaly's global gRPC panic-handler middleware (`internal/grpc/middleware/panichandler`) and converted into an `Internal` gRPC error for that call: [5](#0-4) 

While this stops the panic from crashing the whole Gitaly process, it still denies retrieval of *all* references in the affected table/repository for that RPC — analogous to the reported bug where one bad participant caused the entire shared `distribute` operation to revert for every other legitimate participant. Because the corrupted reftable file persists on disk, this denial is not a one-off: every subsequent RPC that needs to read references from that reftable-backed repository (e.g., ref listing, ref lookups used by many other RPCs) will repeatedly panic/fail until the corrupted table is fixed or removed, producing a persistent, repository-scoped DoS.

### Likelihood Explanation
Reachability is limited by how easily an ordinary user can get a genuinely malformed reftable block written or introduced (e.g., via storage-layer replication, corrupted transfer, or a future/alternate write path that doesn't go through git's own reftable writer with its own invariants). I was not able to fully confirm, within the available indexing, a direct end-to-end path from an unauthenticated push/fetch RPC field to an attacker-fully-controlled byte sequence reaching `getRefsFromBlock` (git's own reftable writer is expected to produce well-formed blocks). This uncertainty should be resolved by tracing all callers of `ParseTable`/`GetReferences` (seen referenced from `internal/gitaly/storage/storagemgr/partition/reftable.go`, `transaction_manager.go`, `internal/backup/repository.go`, and `internal/gitaly/service/repository/replicate.go`) to determine whether any of them process reftable bytes originating from replication payloads, backups, or other repository-import paths that are not first validated/rewritten by git itself.

### Recommendation
- In `getVarInt`, `getRefsFromBlock`, and `parseRefBlock`, validate every decoded length/offset against the actual remaining buffer size (`len(src)`, `b.RestartStart`, `b.FullBlockSize`) *before* using it to slice, returning a parse error instead of allowing an out-of-bounds slice expression to panic.
- Wrap `getRefsFromBlock`/`GetReferences` parsing in a recovered inner call (or add exhaustive bounds checks) so that a single malformed block returns a scoped error rather than panicking, and consider isolating/skip-ping the malformed block instead of failing the whole table read, to avoid an all-or-nothing failure mode.
- Add fuzz/unit tests for `getRefsFromBlock`/`getVarInt` with truncated and adversarial length fields (similar to the existing `pktline` truncation tests) to ensure length values are always checked against the buffer prior to slicing.

### Proof of Concept
Not independently verified end-to-end in this environment (index does not expose the full byte layout / a runnable harness). A minimal reproduction would construct a reftable file whose ref-block encodes a `suffixLength` (or symref `size`) varint value larger than the remaining bytes in the block/table buffer, then call `reftable.ParseTable(...).GetReferences()`. Given the code shown above, this triggers `src[idx:idx+suffixLength]` (or the symref equivalent) to panic with "slice bounds out of range", which propagates up as an aborted/`Internal`-erroring RPC on any code path that reads references from that reftable.

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

**File:** internal/git/reftable/reftable.go (L244-253)
```go
		extra := (suffixLength & 0x7)
		suffixLength >>= 3

		refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
		idx = idx + suffixLength

		idx, updateIndexDelta, err = t.getVarInt(src, idx, b.FullBlockSize)
		if err != nil {
			return nil, fmt.Errorf("getting update index delta: %w", err)
		}
```

**File:** internal/git/reftable/reftable.go (L266-293)
```go
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

**File:** internal/git/reftable/reftable.go (L305-327)
```go
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
}
```

**File:** internal/grpc/middleware/panichandler/panic_handler.go (L49-63)
```go
func handleCrash(logger log.Logger, grpcMethodName string, handler PanicHandler) {
	if r := recover(); r != nil {
		logger.WithFields(log.Fields{
			"error":     r,
			"method":    grpcMethodName,
			"backtrace": string(debug.Stack()),
		}).Error("grpc panic")

		handler(grpcMethodName, r)

		for _, fn := range additionalHandlers {
			fn(grpcMethodName, r)
		}
	}
}
```

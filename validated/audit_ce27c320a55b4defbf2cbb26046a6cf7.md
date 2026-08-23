### Title
Integer underflow from unchecked multiplication in reftable block parsing causes out-of-bounds panic (DoS) - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.parseRefBlock` and `Table.getRefsFromBlock` in `internal/git/reftable/reftable.go` parse the binary "reftable" ref-storage format without validating length/offset fields extracted from the file against the buffer bounds before doing unsigned arithmetic on them. This mirrors the reported Solidity bug class (unchecked multiplication/arithmetic on attacker-influenced values overflowing/underflowing the safe numeric range and corrupting a downstream calculation), except here the corrupted value is used directly as a Go slice index, so the failure mode is an out-of-bounds slice panic rather than a silently wrong price.

### Finding Description
`parseRefBlock` computes the restart-table start offset as:
```go
b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
``` [1](#0-0) 

`b.RestartCount` is a `uint16` read directly from the file with `binary.Read` and is entirely attacker/file controlled, up to `0xFFFF`. `blockStart`, `currentBS` are `uint` (unsigned) values derived from the block header (`extractBlockLen`, itself parsed from file bytes with `big.NewInt(...).SetBytes(...)`) with no upper bound check relative to `3*RestartCount`. Because `uint` arithmetic in Go wraps on underflow, if `3*uint(b.RestartCount)` exceeds `blockStart+currentBS-2`, the subtraction underflows to a huge value near `MaxUint`, corrupting `RestartStart`.

That corrupted `RestartStart` is then used as the loop bound in `getRefsFromBlock`:
```go
for idx < b.RestartStart {
    ...
    idx, prefixLength, err = t.getVarInt(src, idx, b.RestartStart)
    ...
    refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
    idx = idx + suffixLength
    ...
}
``` [2](#0-1) 

`getVarInt`'s bounds check (`start > blockEnd`) becomes meaningless once `blockEnd` (here `b.RestartStart`) is itself a huge wrapped value, so `idx`, `prefixLength`, and `suffixLength` (all attacker-controlled, unsigned, and combined via addition/shifts in `getVarInt`, e.g. `val = ((val + 1) << 7) | ...`) can walk `idx` far past `len(src)`. The subsequent slice expressions `prefix[:prefixLength]`, `src[idx:idx+suffixLength]`, and `src[idx : idx+uint(hashSize)]` will then panic with "index out of range" or "slice bounds out of range" once the (wrapped/attacker-controlled) index exceeds `len(src)`, since none of these paths re-validate against the actual buffer length — only against the already-corrupted `blockEnd`/`RestartStart`/`FullBlockSize` values.

### Impact Explanation
`Table.GetReferences` is the entry point that parses an on-disk `.ref` reftable file end-to-end (`ParseTable` → `GetReferences` → `parseRefBlock` → `getRefsFromBlock`) [3](#0-2) . This code path is used whenever Gitaly's reference backend is `reftable` and references need to be enumerated (e.g. servicing ref listing during common ref-reading RPCs and internal Git operations). A crafted or corrupted reftable file placed into a repository's `reftable/` directory (e.g. through repository import/creation-from-bundle/snapshot restoration, or replication of a tampered repository) can drive `RestartCount`, `currentBS`, and varint-derived lengths to values that trigger the underflow and subsequent out-of-bounds slice access. In Go, an out-of-bounds slice access is an unrecovered panic unless caught by a `recover()` somewhere in the calling goroutine's stack; if not recovered, it can crash the serving goroutine and, for panics that escape gRPC handler recovery middleware, potentially the whole `gitaly` process, denying service to all repositories on that node.

### Likelihood Explanation
Exploitability depends on an unprivileged actor being able to get a malformed `.ref` file onto disk in a location Gitaly will later parse via `ParseTable`/`GetReferences`, e.g. via repository creation from a bundle/snapshot that includes raw `reftable/` directory contents, or via fork/replication of a repository containing a tampered table. Because reftables are normally generated exclusively by Git itself and not accepted as raw wire input in most RPCs, likelihood is moderate rather than trivial — it requires a code path that ingests raw repository files (bundle/snapshot import, replication) rather than a simple `git push`. I could not confirm within the indexed code which exact RPC(s) allow uploading a raw, attacker-crafted `reftable/*.ref` file onto disk; this would need further verification in a live session.

### Recommendation
- Validate all length/offset fields read from the reftable block header against the actual buffer length (`len(src)`) before performing arithmetic on them, not merely against other file-derived values (`blockEnd`, `FullBlockSize`).
- Compute `RestartStart` using bounds-checked subtraction (verify `blockStart+currentBS-2 >= 3*uint(b.RestartCount)` before subtracting) and reject the block with an error if the check fails, mirroring the recommended fix pattern of validating quantities before use rather than trusting wrapped/overflowed arithmetic results.
- Add explicit bounds checks in `getVarInt` and `getRefsFromBlock` before every slice operation (`prefix[:prefixLength]`, `src[idx:idx+suffixLength]`, `src[idx:idx+hashSize]`) so malformed files return a parse error instead of panicking.
- Consider wrapping reftable parsing in a `recover()` at the RPC boundary as defense-in-depth against any remaining panics from malformed on-disk state.

### Proof of Concept
Conceptual (not executed): craft a `.ref` reftable file whose ref-block header encodes a `currentBS` and trailing `RestartCount` (uint16, big-endian, at `currentBS-2`) such that `3*RestartCount > blockStart+currentBS-2`. Placing this crafted table where Gitaly reads it (via an import/bundle/snapshot path that writes raw `reftable/` files) and then invoking a reference-listing operation that calls `Table.GetReferences()` will drive `b.RestartStart` to underflow to a near-`MaxUint` value; the subsequent `for idx < b.RestartStart` loop will read `src` far past its length in `getVarInt`/`getRefsFromBlock`, panicking with an out-of-bounds slice access. I was not able to fully trace, within the indexed subset of the codebase, the specific RPC/import path that allows an unprivileged caller to place such a file on disk — this would require a live Devin session with full repository access to confirm the exact reachable RPC surface.

### Citations

**File:** internal/git/reftable/reftable.go (L228-298)
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
		}

		prefix = refname

		references = append(references, reference)
	}
```

**File:** internal/git/reftable/reftable.go (L320-324)
```go
	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

**File:** internal/git/reftable/reftable.go (L330-370)
```go
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

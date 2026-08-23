### Title
Out-of-bounds slice access when parsing a crafted `.ref` reftable block causes a Gitaly RPC handler to panic (DoS) - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.getVarInt` in `internal/git/reftable/reftable.go` reads `src[start]` before validating that `start` is inside the bounds of `src`, exactly mirroring the analog bug class: the Solidity `checkWhitelisted` function indexed `whitelistStatuses[0]` before checking the array was non-empty. Here, a maliciously crafted reftable block can push `start`/`idx` past the end of the in-memory buffer, and the very next byte read panics with "index out of range" instead of returning a handled error.

### Finding Description
`getVarInt` computes the first byte unconditionally: [1](#0-0) 
Note that `val = uint(src[start]) & 0x7f` executes with no bounds check on `start` against `len(src)`; the only bounds check performed (`start > blockEnd`) happens *after* the first byte has already been consumed, and only guards continuation bytes, not the initial read. `blockEnd` itself is derived from attacker-influenced fields (`RestartStart`/`FullBlockSize`) computed in `parseRefBlock`, which are themselves derived from `extractBlockLen` and `RestartCount` read straight out of file bytes without adequate cross-validation against the actual buffer length: [2](#0-1) 

`getRefsFromBlock` repeatedly calls `getVarInt` and then slices `src[idx:idx+suffixLength]` / `src[idx:idx+uint(hashSize)]` using values it just parsed, with no check that `idx+length <= len(src)`: [3](#0-2) [4](#0-3) 

A specially crafted `.ref` table file (oversized `RestartCount`, a corrupted `block_len`, or truncated data) can drive `idx`/`start` beyond `len(src)`, causing a Go runtime panic (slice/index out of range) inside `GetReferences()`.

`GetReferences()` is invoked from `internal/backup/repository.go`, i.e., on the backup/restore code path, which is one of the explicitly in-scope "archive or bundle extraction"-style flows: a repository backup or restore operation reads reftable files from disk and parses them with this code. If an attacker can influence the reftable files that end up being processed during a backup or restore (e.g., via a crafted repository import/bundle that populates the reftable directory, or a corrupted/tampered table produced through concurrent/aborted write paths), the parser will panic instead of returning a structured error.

### Impact Explanation
A panic inside a Gitaly RPC handler (backup/restore of a repository) crashes the goroutine servicing that RPC. Depending on recover/middleware behavior this can terminate or destabilize the serving process for that request, resulting in denial of service. No corruption of unrelated data or privilege escalation results, but the affected operation fails abnormally rather than with a controlled error, and repeated crafted inputs can be used to repeatedly disrupt backup/restore RPCs — directly analogous to the "reseed" init function reverting unexpectedly.

### Likelihood Explanation
The likelihood requires the attacker to supply or influence a reftable (`.ref`) file that later gets parsed by `GetReferences`. This is more constrained than a fully open user-triggered RPC path because reftable files are normally produced by Gitaly/Git itself, not directly by end users. It is most plausible via a corrupted/incompletely-written table (e.g., from an interrupted transaction, or a maliciously provided repository during import/restore that includes a `reftable/` directory with a hand-crafted `.ref` file). Because this requires either data corruption or attacker-supplied repository content specifically feeding the reftable backend, likelihood is Low-to-Medium.

### Recommendation
Add explicit bounds checks before every raw byte/slice access derived from parsed lengths in `getVarInt` and `getRefsFromBlock`:
- In `getVarInt`, validate `start < uint(len(src))` before the initial `src[start]` read, and return an error instead of panicking.
- In `getRefsFromBlock`, after each `getVarInt` call and before every `src[idx:idx+n]` slice, verify `idx+n <= uint(len(src))` (and `<= b.FullBlockSize`) and return a parsing error otherwise, mirroring the fix pattern in the report (guard before indexing, don't trust the loop invariant alone).
- Add fuzz/unit tests that feed truncated or corrupted reftable blocks to `GetReferences` to ensure it returns errors rather than panicking.

### Proof of Concept
Construct a `.ref` table file whose ref block's `RestartCount`/`block_len` bytes are set so that `RestartStart` (or `FullBlockSize`) is computed larger than the physical block content in the underlying file, and truncate the physical data early. Load it via `reftable.ParseTable(path)` followed by `t.GetReferences()`:
```go
tbl, _ := reftable.ParseTable(pathToCraftedRefFile)
_, err := tbl.GetReferences() // panics: index out of range, rather than returning err
```
The panic occurs in `getVarInt`/`getRefsFromBlock` because `idx`/`start` exceeds `len(src)` while no length check gates the read, at: [5](#0-4) [6](#0-5)

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

**File:** internal/git/reftable/reftable.go (L230-253)
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

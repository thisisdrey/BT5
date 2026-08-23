### Title
Unsigned integer underflow when parsing reftable block header causes out-of-bounds panic / DoS - (File: internal/git/reftable/reftable.go)

### Summary
`Table.parseRefBlock` computes block boundaries using unsigned (`uint`) subtraction on values read directly from a `.ref` reftable file (`currentBS`, `RestartCount`). If the file contains a small/crafted `block_len` or an oversized restart count, the subtraction underflows and wraps to a near-`MaxUint` value, which is then used as a slice index/bound in `getRefsFromBlock`, causing an out-of-bounds slice access panic. This is directly analogous to the reported Solidity bug class: an unchecked `a - b` where `b` can exceed `a`, producing an invalid (here, wrapped-around) result instead of being clamped or rejected.

### Finding Description
`extractBlockLen` reads a 3-byte, attacker-controlled block length (`currentBS`) straight out of the reftable bytes with no lower-bound validation: [1](#0-0) 

`parseRefBlock` then reads the restart count from `src[blockStart+currentBS-2:]` and computes the restart-table start with unsigned subtraction: [2](#0-1) 

Both `blockStart+currentBS-2` and `blockStart + currentBS - 2 - 3*uint(b.RestartCount)` are computed in Go's unsigned `uint` type. If `currentBS < 2` (e.g. attacker sets block length to `0` or `1`), or if `3*RestartCount` exceeds `blockStart+currentBS-2` (achievable since `RestartCount` is an independently-controlled `uint16` read from the file, up to 65535), the subtraction underflows/wraps instead of erroring — exactly the same bug class as the Solidity report's `ud(supply - p)` reverting/underflowing when `p > supply`, except here it silently wraps due to Go's unsigned integer semantics rather than reverting, which is arguably worse because there is no revert/guard at all.

The corrupted, huge `RestartStart` value is then used as both a loop bound and a slice index in `getRefsFromBlock`: [3](#0-2) 

Because `idx < b.RestartStart` will be true for essentially the whole in-memory buffer, and `src[idx:idx+suffixLength]` / `getVarInt` will run past the actual block/file bounds, this produces an out-of-bounds slice-index panic once `idx` exceeds `len(src)`.

Reachability: reftable `.ref` files are not cryptographically or structurally validated beyond a header/footer checksum (`parseHeader`/`parseFooter`) — the checksum covers only the fixed header/footer, not the variable-length block contents that `parseRefBlock`/`getRefsFromBlock` parse. Table files reach this parser via:
- `internal/gitaly/storage/storagemgr/partition/reftable.go`, which calls `reftable.ParseTable` on tables written into a transaction's snapshot directory during WAL replay/resequencing. [4](#0-3) 
- `internal/gitaly/service/repository/replicate.go`, which extracts a full repository (including a `reftable/` directory containing raw `.ref` files) from a `GetSnapshot` tar stream via `extractTarToDirectory`, with only path-traversal checks on `header.Name`/symlinks — no validation of reftable internal block structure. [5](#0-4) 

A repository whose reftable blocks are crafted this way (e.g. via a malicious/compromised source of a replication snapshot, backup restore, or any code path that writes/imports raw `.ref` bytes) will crash the goroutine handling table parsing when `GetReferences`/`stageTables` is subsequently invoked on it.

### Impact Explanation
This is a Denial-of-Service vector: a crafted reftable file causes an out-of-bounds slice-index panic in Go, which — unless recovered by a top-level panic handler — terminates request handling (and potentially crashes the `gitaly` process if the panic occurs outside a recovering goroutine wrapper, e.g. inside WAL/transaction processing paths). Given reftable is Gitaly's reference-storage backend, corruption/parsing failures here affect read/write reference operations and internal WAL replay, i.e., core repository functionality.

### Likelihood Explanation
Medium: an attacker needs to get a specially crafted reftable file onto a target Gitaly node's storage, most plausibly through a replication/snapshot flow (`GetSnapshot` extraction) or backup/restore/import path that lands raw bytes into a repository's `reftable/` directory without semantic validation of the block internals. Standard `git push` flows would produce reftables via `git` itself and are less likely to hit this directly, but any RPC/flow that transplants repository files verbatim (replication, backup restore, `CreateRepositoryFromSnapshot`-style operations) is a realistic trigger.

### Recommendation
Add explicit bounds checks before performing the subtractions in `parseRefBlock`, mirroring the fix pattern from the report (clamp/guard instead of allowing unsigned wraparound):

```diff
 func (t *Table) parseRefBlock(src []byte, headerOffset, blockStart, blockEnd uint) ([]git.Reference, error) {
 	currentBS := t.extractBlockLen(src, blockStart+headerOffset)
+	if currentBS < 2 {
+		return nil, fmt.Errorf("block length too small: %d", currentBS)
+	}
 
 	fullBlockSize := t.blockSize
 	...
 	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
 		return nil, fmt.Errorf("reading restart count: %w", err)
 	}
 
+	restartTableSize := 3 * uint(b.RestartCount)
+	if restartTableSize+2 > currentBS {
+		return nil, fmt.Errorf("restart count %d exceeds block size %d", b.RestartCount, currentBS)
+	}
 	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)
```

Additionally, add bounds validation on all `src[idx:idx+n]` slicing operations in `getRefsFromBlock`/`getVarInt` (checking against `len(src)` / `blockEnd`) so malformed files return a parse error rather than panicking.

### Proof of Concept
1. Take a valid reftable `.ref` file (as written by `git`/Gitaly).
2. Locate a `'r'`-type ref block and overwrite its 3-byte `block_len` field (read by `extractBlockLen`) with a value `< 2` (e.g. `0x000000`), or leave `block_len` intact but set the trailing restart-count `uint16` field to a large value (e.g. `0xFFFF`) so that `3*RestartCount > blockStart+currentBS-2`.
3. Feed this file through `reftable.ParseTable` + `Table.GetReferences()` (or trigger it via a replication/snapshot flow that plants this file into a repository's `reftable/` directory).
4. Observe that `parseRefBlock` computes an underflowed/huge `RestartStart`, and the subsequent loop in `getRefsFromBlock` panics with `index out of range` when slicing `src` beyond its length.

Note: I was unable to fully trace whether a fully unauthenticated, ordinary `git push` alone can synthesize a reftable file with these exact malformed byte values (git's own reftable writer would not normally produce them), so the strongest confirmed vector is via file-import/replication/snapshot-restore paths that place raw `.ref` bytes on disk without validating reftable internal block structure; a Devin session with full repo/test access would be needed to confirm the precise minimal RPC-only trigger path.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

**File:** internal/git/reftable/reftable.go (L222-248)
```go
func (t *Table) getRefsFromBlock(src []byte, b *block) ([]git.Reference, error) {
	var references []git.Reference

	prefix := ""

	// Skip the block_type and block_len
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

**File:** internal/git/reftable/reftable.go (L305-326)
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
```

**File:** internal/gitaly/storage/storagemgr/partition/reftable.go (L93-110)
```go
		if err := func() (returnedErr error) {
			table, err := reftable.ParseTable(filepath.Join(r.snapshotRepoPath, "reftable", originalTableName.String()))
			if err != nil {
				return fmt.Errorf("parse table: %w", err)
			}

			defer func() {
				if err := table.Close(); err != nil {
					returnedErr = errors.Join(returnedErr, fmt.Errorf("close: %w", err))
				}
			}()

			if err := table.PatchUpdateIndexes(
				resequencedTableName.MinUpdateIndex,
				resequencedTableName.MaxUpdateIndex,
			); err != nil {
				return fmt.Errorf("patch update indexes: %w", err)
			}
```

**File:** internal/gitaly/service/repository/replicate.go (L314-345)
```go
// extractTarToDirectory extracts a tar archive to the specified directory using Go's tar package
func (s *server) extractTarToDirectory(ctx context.Context, reader io.Reader, targetDir string) error {
	targetDir = filepath.Clean(targetDir)
	tarReader := tar.NewReader(reader)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		header, err := tarReader.Next()
		if err == io.EOF {
			break // End of archive
		}
		if err != nil {
			return fmt.Errorf("reading tar header: %w", err)
		}

		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(targetPath, os.FileMode(header.Mode)); err != nil {
				return fmt.Errorf("creating directory %s: %w", targetPath, err)
			}
```

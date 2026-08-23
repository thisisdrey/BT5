### Title
Unsigned Integer Underflow in Reftable Block-Length Parsing Leads to Out-of-Bounds Slice Index / Handler Crash - ([File: internal/git/reftable/reftable.go])

### Summary
`Table.parseRefBlock` reads an attacker-influenced 3-byte block-length field from an on-disk `.ref` (reftable) file and uses it, without a minimum-value check, in unsigned pointer arithmetic (`blockStart+currentBS-2`) to slice into the file's byte buffer. Because `currentBS` is a `uint` sourced directly from file bytes, a crafted value smaller than 2 causes the subtraction to underflow to a value near `math.MaxUint`, producing a huge slice index. This is the same bug class as the reported Monero issue — an untrusted, insufficiently-bounded integer used directly as an array/slice index, causing the effective index to become nonsensical (there, negative via signed-char cast; here, "negative" via unsigned wraparound) — resulting in undefined/out-of-bounds memory access (in Go's case, a runtime panic).

### Finding Description
`extractBlockLen` reads a 3-byte, file-controlled big-endian integer directly from the reftable bytes with no minimum bound enforced: [1](#0-0) 

`parseRefBlock` then uses this value (`currentBS`) to compute the restart-count offset and the start of the restart table by subtracting a constant `2` from it: [2](#0-1) 

All quantities here (`blockStart`, `currentBS`, `b.RestartStart`) are unsigned (`uint`). If `currentBS` is `0` or `1` — a value fully controlled by the bytes stored in the `.ref` table file at `blockStart+headerOffset+1..+4` — the expression `blockStart+currentBS-2` underflows and wraps to a value close to `math.MaxUint64`. This is passed straight into `src[blockStart+currentBS-2:]`, which is a Go slice bounds check that will panic with "slice bounds out of range" rather than silently reading wrong memory (as in the C/C++ analog), but it is the direct structural equivalent of the reported issue: an untrusted integer flows into pointer/index arithmetic without a lower-bound validation, producing an out-of-range access.

The same missing-bounds-check pattern also exists one layer down in `getVarInt`, which dereferences `src[start]` before validating `start` against `blockEnd` on the very first read: [3](#0-2) 

This reftable parser is Gitaly's from-scratch (non-libgit) implementation of the reftable ref-storage format used by the WAL/partition backend:

Reftable files (`*.ref`) under a repository's `reftable/` directory are read via `ParseTable` whenever ref data must be reconstructed from on-disk tables (e.g., during partition/transaction replay, housekeeping, or repository info collection): [4](#0-3) 

### Impact Explanation
If a `.ref` table file with a corrupted/adversarial block-length byte (0 or 1) enters a repository's `reftable/` directory — e.g., via repository import/fork/restore flows that place raw filesystem content into a repository's storage path rather than regenerating it through Git/Gitaly's own reftable writer — later reads of that table via `ParseTable`/`GetReferences` will panic instead of returning a parse error. This is a Denial-of-Service of the RPC handler that triggers the read (any ref-listing or transaction-replay code path that parses the affected table).

### Likelihood Explanation
Exploitability depends on there being a code path where reftable file *bytes* are taken from untrusted input verbatim (e.g., import of a repository bundle/archive/snapshot containing a `reftable/` directory) rather than always being generated internally by Gitaly's own writer, which would compute `currentBS` correctly. I was not able to fully verify, within the scope of this investigation, which repository-import/restore RPCs copy raw `reftable/` file bytes without re-validating/re-generating them; this is the main uncertainty in assessing real-world reachability. Regardless of the exact entry point, the underlying code has no defensive lower-bound check on a file-derived integer before using it in subtraction that can underflow — a genuine correctness/robustness gap that mirrors the reported bug class.

### Recommendation
- In `extractBlockLen`/`parseRefBlock`, validate that `currentBS >= 2` (and reasonably bounded by `blockEnd-blockStart`) before computing `blockStart+currentBS-2`; return a parse error instead of proceeding.
- In `getVarInt`, check `start <= blockEnd` before the first dereference of `src[start]`, not only after incrementing.
- Add fuzz coverage for `reftable.ParseTable`/`GetReferences` against malformed `.ref` files (mirroring the Monero `portable_storage` fuzzer style referenced in the external report) to catch this class of unsigned-underflow/index issues before release.

### Proof of Concept
Conceptual (Go), demonstrating the underflow in isolation:
```go
// currentBS is read from file bytes fully controlled by whoever supplies the .ref file.
var blockStart uint = 4
var currentBS uint = 1 // attacker sets the 3-byte block length field to 1

// blockStart + currentBS - 2 underflows to a value near math.MaxUint64
idx := blockStart + currentBS - 2
_ = src[idx:] // panics: slice bounds out of range
```
A full end-to-end PoC would require constructing a `reftable/`-backed repository whose `.ref` file's block-length byte(s) are set to `0x00 0x00 0x00`/`0x00 0x00 0x01` and feeding it through whatever import/restore path places raw reftable bytes into a repository, then triggering a ref read (`ParseTable` → `GetReferences`). I could not confirm within this investigation which specific RPC performs a raw, unvalidated copy of `reftable/` file contents, so this PoC step remains unverified.

### Citations

**File:** internal/git/reftable/reftable.go (L198-201)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}
```

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

**File:** internal/git/reftable/reftable.go (L449-488)
```go
func ParseTable(absolutePath string) (_ *Table, returnedErr error) {
	src, err := os.Open(absolutePath)
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}

	defer func() {
		if returnedErr != nil {
			if err := src.Close(); err != nil {
				returnedErr = errors.Join(returnedErr, fmt.Errorf("close: %w", err))
			}
		}
	}()

	t := &Table{src: src, absolutePath: absolutePath}

	var h header
	if err := parseHeader(src, &h); err != nil {
		return nil, fmt.Errorf("parse header: %w", err)
	}

	footerOffset, err := src.Seek(int64(-h.Version.FooterSize()), io.SeekEnd)
	if err != nil {
		return nil, fmt.Errorf("seek footer: %w", err)
	}

	t.footerOffset = uint(footerOffset)

	if err := parseFooter(src, &t.footer); err != nil {
		return nil, fmt.Errorf("parse footer: %w", err)
	}

	if h != t.footer.header {
		return nil, fmt.Errorf("footer doesn't match header")
	}

	t.blockSize = parseUint24(t.footer.BlockSize)

	return t, nil
}
```

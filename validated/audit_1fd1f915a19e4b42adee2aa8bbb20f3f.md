Based on my investigation, `reftable.ParseTable`/`Table.GetReferences` is invoked from `reftableRecorder.stageTables` in `internal/gitaly/storage/storagemgr/partition/reftable.go` (called during transaction commit/WAL staging), which runs from the `TransactionManager`'s background apply/commit path — outside of a gRPC handler's call stack and thus **not protected by the `panichandler` gRPC middleware** (`internal/grpc/middleware/panichandler/panic_handler.go`), which only wraps `handler(ctx, req)` inside the RPC interceptor chain.

### Title
Unbounded/unchecked slice indexing in reftable block parser can panic and crash the storage-manager goroutine on a crafted `.ref` table - (File: internal/git/reftable/reftable.go)

### Summary
`Table.getVarInt`, `Table.getRefsFromBlock`, and `Table.parseRefBlock` index into the in-memory table bytes (`src[...]`) using offsets/lengths (`prefixLength`, `suffixLength`, `hashSize`, varint-derived `size`) that are read directly from the table file content without validating that the resulting index stays within `len(src)`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`getVarInt` bounds-checks against `blockEnd`, but that bound is itself computed from attacker-influenced fields (`b.RestartStart`, `b.FullBlockSize`) which are derived from the file's own block-length and restart-count bytes (`extractBlockLen`, `parseRefBlock`) rather than validated against the true buffer size `len(src)`. [4](#0-3)  Once `idx`/`prefixLength`/`suffixLength`/`hashSize` are combined (e.g. `src[idx:idx+suffixLength]`, `src[idx:idx+uint(hashSize)]`, `src[idx:idx+size]`), a crafted or truncated reftable can push these slice expressions past `len(src)`, triggering an unrecovered Go runtime panic (`index out of range`) instead of returning an error. [5](#0-4) [6](#0-5) [7](#0-6) 

This is the same bug class as the Besu advisory: a validation/parsing routine throws an unchecked/unrecovered exceptional condition (there, an unbounded `BigInteger` triggering an uncaught `IllegalArgumentException`; here, unbounded length fields triggering an uncaught Go panic) in a code path that the surrounding caller does not defend against. `GetReferences` is invoked from `reftableRecorder.stageTables`, which is called by the `TransactionManager`'s WAL-staging/commit logic, i.e., a background transaction-processing goroutine rather than directly inside a gRPC handler. [8](#0-7)  Gitaly's own style guide flags exactly this hazard: "every new goroutine has the potential to crash the process... an unrecovered panic can cause the entire process to crash," and it explicitly asks engineers to wrap unsure code with `dontpanic.Go`. [9](#0-8)  The `dontpanic` package exists precisely to guard this class of long-running/background goroutines. [10](#0-9)  There's no evidence in the code I could find that `TransactionManager`'s commit/apply pipeline wraps this call path in a `dontpanic.Try`/`dontpanic.Go`-style recovery.

### Impact Explanation
If reached, an unrecovered panic inside the transaction manager's background apply goroutine would not be caught by the gRPC `panichandler` middleware (that middleware only recovers panics that occur synchronously within an RPC's `handler(ctx, req)` call) [11](#0-10) , and a panic in a goroutine with no `recover()` crashes the entire Gitaly process, taking down all in-flight RPCs on that node — a process-wide denial of service rather than a single-request failure.

### Likelihood Explanation
I was **not able to fully verify** the exact end-to-end reachability from an ordinary user's push/fetch to a state where `GetReferences`/`parseRefBlock` parses attacker-influenced table content, nor whether the reftable byte content being parsed here originates purely from Gitaly's own `git-update-ref`/`git pack-refs` writes (which would only be reachable via file corruption, disk-level races, or Git itself producing an edge case) versus content that a client can directly influence (e.g., by writing crafted table bytes into a quarantine/snapshot directory before commit). This distinction is critical to severity and I could not confirm it with the tools available — a full trace of `stageTables`'s snapshot-repo path and whether that snapshot repo's reftable files can contain client-supplied byte sequences from an untrusted push would be needed.

### Recommendation
Add explicit bounds checks (`idx+length <= len(src)` / `idx+length <= blockEnd`) before every slice operation in `getVarInt`, `getRefsFromBlock`, `extractBlockLen`, and `parseRefBlock`, returning an error instead of allowing a panic. Additionally, wrap the reftable-parsing call inside `stageTables`/`TransactionManager` background goroutines with `dontpanic.Try` (or equivalent `recover()`) so that even if a parsing bug slips through, it degrades to a transaction failure rather than crashing the whole storage-manager process.

### Proof of Concept
I could not construct or verify a concrete PoC without further investigation into how snapshot/quarantine reftable files are produced and whether any of their byte content is attacker-controllable prior to `stageTables` calling `reftable.ParseTable`/`GetReferences`. A background Devin session with filesystem/test access would be needed to (1) confirm the data flow from a crafted push into a `.ref` table file consumed by this parser, and (2) craft a minimal malformed table (e.g. truncated block with an inflated `suffixLength` or symref `size`) to trigger the out-of-bounds panic and confirm whether it crashes the process.

### Citations

**File:** internal/git/reftable/reftable.go (L199-219)
```go
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

**File:** internal/git/reftable/reftable.go (L266-292)
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

**File:** internal/gitaly/storage/storagemgr/partition/reftable.go (L93-97)
```go
		if err := func() (returnedErr error) {
			table, err := reftable.ParseTable(filepath.Join(r.snapshotRepoPath, "reftable", originalTableName.String()))
			if err != nil {
				return fmt.Errorf("parse table: %w", err)
			}
```

**File:** STYLE.md (L549-556)
```markdown
### Goroutine Panic Risks

Additionally, every new goroutine has the potential to crash the process. Any
unrecovered panic can cause the entire process to crash and take out any in-
flight requests (**VERY BAD**). When writing code that creates a goroutine,
consider the following question: How confident are you that the code in the
goroutine won't panic? If you can't answer confidently, use a helper that
recovers panics for you: [`dontpanic.Go`].
```

**File:** internal/dontpanic/retry.go (L1-27)
```go
// Package dontpanic provides function wrappers and supervisors to ensure
// that wrapped code does not panic and cause program crashes.
//
// When should you use this package? Anytime you are running a function or
// goroutine where it isn't obvious whether it can or can't panic. This may
// be a higher risk in long running goroutines and functions or ones that are
// difficult to test completely.
package dontpanic

import (
	"sync"
	"time"

	sentry "github.com/getsentry/sentry-go"
	"gitlab.com/gitlab-org/gitaly/v18/internal/log"
)

// Try will wrap the provided function with a panic recovery. If a panic occurs,
// the recovered panic will be sent to Sentry and logged as an error.
// Returns `true` if no panic and `false` otherwise.
func Try(logger log.Logger, fn func()) bool { return catchAndLog(logger, fn) }

// Go will run the provided function in a goroutine and recover from any
// panics.  If a panic occurs, the recovered panic will be sent to Sentry
// and logged as an error. Go is best used in fire-and-forget goroutines where
// observability is lost.
func Go(logger log.Logger, fn func()) { go Try(logger, fn) }
```

**File:** internal/grpc/middleware/panichandler/panic_handler.go (L20-29)
```go
// UnaryPanicHandler creates a new unary server interceptor that handles panics.
func UnaryPanicHandler(logger log.Logger) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (resp interface{}, err error) {
		defer handleCrash(logger, info.FullMethod, func(grpcMethodName string, r interface{}) {
			err = toPanicError(grpcMethodName, r)
		})

		return handler(ctx, req)
	}
}
```

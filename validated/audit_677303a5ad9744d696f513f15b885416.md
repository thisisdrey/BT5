### Title
Panic-based DoS via unbounded slice indexing in reftable ref-block parsing - (File: internal/git/reftable/reftable.go)

### Summary
`Table.getRefsFromBlock` in the reftable backend parser slices Go byte/string values using length fields (`prefixLength`, `suffixLength`, symref `size`) read directly from an untrusted `.ref` table file, without verifying they fit within the bounds of the existing `prefix` string or the `src` byte slice. This mirrors the reported foundry bug class: an unvalidated length is used to index/slice data, causing a runtime panic when the length exceeds the actual data size.

### Finding Description
`getRefsFromBlock` decodes reference records from a reftable block: [1](#0-0) 

`prefixLength` and `suffixLength` are parsed by `getVarInt`, which only checks that the varint continuation bytes stay within `blockEnd` — it performs no validation of the decoded *value* against the remaining data: [2](#0-1) 

The decoded `prefixLength` is then used to slice the previous reference's `prefix` string (`prefix[:prefixLength]`), and `suffixLength` is used to slice `src[idx:idx+suffixLength]`. If `prefixLength` exceeds `len(prefix)` (e.g., on the very first record, where `prefix == ""`, any nonzero `prefixLength` triggers this), or if `suffixLength`/`idx` combinations exceed `len(src)`, Go's runtime raises `slice bounds out of range`, which is an unrecovered panic in this code path. The same unchecked-length pattern recurs for the symref case (`src[idx : idx+size]` at line 290) and the SHA payload slicing (`src[idx : idx+uint(hashSize)]` at lines 268/274).

This exactly parallels the alloy-dyn-abi panic in the report: a length value from untrusted input is used to slice data without a preceding bounds check, producing a native panic instead of a graceful decode error.

### Impact Explanation
`getRefsFromBlock` is reached through `Table.GetReferences`/`parseRefBlock`, which parses `.ref` reftable files written under a repository's `reftable/` directory. Reftable files are read by Gitaly's reftable backend (`git.ReferenceBackendReftables`) whenever references are enumerated or transactions replay reference state (e.g. `internal/gitaly/storage/storagemgr/partition/reftable.go` uses `reftable.ParseTable`/`ReadTablesList` during transaction staging, and `RepositoryInfo`/`ReferencesInfoForRepository` reads tables during replication as shown in `internal/git/stats/repository_info.go`). A malformed reftable file — introduced via a crafted fork/import, a crafted `ReplicateRepository`/`CreateRepositoryFromBundle` payload, or any path where Gitaly ingests repository data whose reference backend is reftables — can trigger this panic. Because gRPC handlers run under `panichandler.UnaryPanicHandler`/`StreamPanicHandler` (recovering to an `Internal` error rather than crashing the whole process), the practical impact is degraded to request-scoped failure of the RPC handling the malformed table rather than a full-process crash — still a denial-of-service of that specific operation (e.g. reference listing, replication, or transaction commit/staging), and a genuine bug (unhandled internal error) though bounded by the panic recovery middleware. [3](#0-2) 

### Likelihood Explanation
Reaching this code requires the reftable reference backend to be in use and a reftable file containing an attacker-influenced/corrupted encoding to be read by Gitaly (e.g. via repository creation from a bundle/replication source, or fork/import flows that copy reference data). This is plausible in environments using the reftable backend, but requires the attacker to be able to supply or corrupt repository reftable data that Gitaly subsequently parses — a moderate-likelihood, ordinary-repository-data path rather than a purely internal one.

### Recommendation
Add explicit bounds checks in `getRefsFromBlock` before slicing:
- Verify `prefixLength <= len(prefix)` before `prefix[:prefixLength]`.
- Verify `idx+suffixLength <= uint(len(src))` before `src[idx:idx+suffixLength]`.
- Apply the same bound check for the SHA hash slices (`idx+hashSize <= len(src)`) and the symref target slice (`idx+size <= len(src)`).
Return a descriptive parse error (e.g. `fmt.Errorf("prefix length %d exceeds available prefix")`) instead of allowing the runtime to panic, consistent with the error-returning style already used elsewhere in this function.

### Proof of Concept
Craft a reftable `.ref` file whose first ref-block record encodes a nonzero `prefixLength` varint (e.g. value `1`) while `prefix` is still `""` (the initial value at the start of `getRefsFromBlock`). Passing this file to `reftable.ParseTable(...).GetReferences()` (or triggering any Gitaly operation that reads it, such as replication of a repository using the reftable backend) causes `prefix[:1]` to execute `""[:1]`, producing a `runtime error: slice bounds out of range [:1] with capacity 0` panic inside the request handling goroutine. I was not able to run this against a live Gitaly instance to confirm end-to-end reachability (e.g. exact RPC-triggerable path with an unauthenticated/ordinary user), so this should be validated with an actual crafted repository and a targeted unit test on `getRefsFromBlock`/`ParseTable` before treating it as fully confirmed.

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

**File:** internal/grpc/middleware/panichandler/panic_handler.go (L20-40)
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

// StreamPanicHandler creates a new stream server interceptor that handles panics.
func StreamPanicHandler(logger log.Logger) grpc.StreamServerInterceptor {
	return func(srv interface{}, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) (err error) {
		defer handleCrash(logger, info.FullMethod, func(grpcMethodName string, r interface{}) {
			err = toPanicError(grpcMethodName, r)
		})

		return handler(srv, stream)
	}
}
```

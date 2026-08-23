### Title
Unhandled `panic()` on out-of-range `SortBy` enum value crashes Gitaly RPC handler - (File: internal/gitaly/service/ref/find_local_branches.go)

### Summary
`FindLocalBranches` (RefService) calls `parseSortKey()` to translate the client-supplied `SortBy` field into a `--sort=` argument for `git for-each-ref`. The function's `switch` statement only handles the three defined enum values (`NAME`, `UPDATED_ASC`, `UPDATED_DESC`) and falls through to `panic("never reached")` for anything else. Because `SortBy` is a protobuf enum transmitted over the wire as a plain `int32`, proto3 does not validate that the received value matches one of the declared enum constants, so an ordinary, unauthenticated-at-the-application-layer gRPC client can send an arbitrary integer (e.g. `999`) in this field and reach the `panic`, crashing the Gitaly process — directly analogous to the reported `log.Panicf`-on-malformed-input DoS in `consortium-node`.

### Finding Description [1](#0-0) 

```go
func parseSortKey(sortKey gitalypb.FindLocalBranchesRequest_SortBy) string {
	switch sortKey {
	case gitalypb.FindLocalBranchesRequest_NAME:
		return "refname"
	case gitalypb.FindLocalBranchesRequest_UPDATED_ASC:
		return "committerdate"
	case gitalypb.FindLocalBranchesRequest_UPDATED_DESC:
		return "-committerdate"
	}

	panic("never reached") // famous last words
}
```

`parseSortKey` is called from the request-handling path of the exported `FindLocalBranches` RPC: [2](#0-1) 

`FindLocalBranches` is invoked directly with the client-supplied `in.GetSortBy()` value, without any prior validation that the underlying int32 is one of the three declared enum constants: [3](#0-2) 

Protobuf enums are represented on the wire as varints; proto3 unmarshalling does not reject unknown enum values (they are preserved as-is on the Go int32-based enum type). This means the comment `// famous last words` is accurate but wrong in spirit — the "never reached" branch *is* reachable by any caller who sets `SortBy` to a value other than 0, 1, or 2.

This is the same bug class as the external report: an externally-controlled field value that isn't a member of an expected enumeration/allow-list reaches a `panic()` (functionally equivalent to `log.Panicf`) instead of being rejected with a structured error, causing the serving goroutine (and, depending on gRPC panic-recovery configuration, potentially the whole process) to crash.

### Impact Explanation
`FindLocalBranches` is a standard accessor RPC exposed to any client authorized to call Gitaly's RefService (i.e., any ordinary GitLab user/host performing a normal repository browsing/listing operation, not a privileged operator). A single crafted request with an out-of-range `SortBy` value causes an unrecovered panic in the RPC-handling goroutine. Depending on whether gRPC panic-recovery middleware converts this into an `Internal` error versus crashing the whole `gitaly-server`/`praefect` process, this can result in denial of service for one or more repositories/nodes, matching the "Impact: A:C" DoS classification of the referenced report.

### Likelihood Explanation
Likelihood is high: no special privileges, authentication bypass, or malicious peer/node collusion is needed — a standard, low-effort malformed field in a routinely-issued RPC (`FindLocalBranchesRequest.SortBy`) is sufficient. The only requirement is the ability to invoke `FindLocalBranches`, which is a common accessor RPC used by GitLab itself for branch listing.

### Recommendation
Replace the `panic("never reached")` fallback with proper input validation: return a `structerr.NewInvalidArgument` (consistent with how the sibling function `FindLocalBranches` already validates `ExcludePatterns`, see lines 22-33 of the same file) for any `SortBy` value that doesn't match a known enum constant, instead of panicking. Audit the rest of the codebase for other `switch` statements over protobuf enum fields that use a `panic()`/`log.Panic` default branch instead of returning a structured error, since the same wire-level enum-validation gap likely applies elsewhere.

### Proof of Concept
1. Construct a `FindLocalBranchesRequest` protobuf message with a valid `Repository` field and set `SortBy` to a raw integer not defined in `gitalypb.FindLocalBranchesRequest_SortBy` (e.g., `999`). Because it's encoded as a plain varint on the wire, gRPC/protobuf will not reject it during unmarshalling.
2. Send this request to Gitaly's `RefService/FindLocalBranches` endpoint.
3. `FindLocalBranches` → `findLocalBranches` → `parseSortKey(in.GetSortBy())` is invoked with the value `999`, none of the three `case` branches match, and execution falls into `panic("never reached")`, crashing the handling goroutine/process.

Note: I was unable to fully verify in this session whether Gitaly's gRPC server wraps handlers with a panic-recovery interceptor that would downgrade this to a per-request `Internal` error rather than crashing the entire process; this would affect the ultimate severity assessment (per-request DoS vs. full-process crash) and should be confirmed by inspecting the gRPC server/interceptor setup (e.g., under `internal/gitaly/server` or `internal/grpc/middleware`) in a follow-up session with fuller repository access.

### Citations

**File:** internal/gitaly/service/ref/find_local_branches.go (L17-40)
```go
func (s *server) FindLocalBranches(in *gitalypb.FindLocalBranchesRequest, stream gitalypb.RefService_FindLocalBranchesServer) error {
	if err := s.locator.ValidateRepository(stream.Context(), in.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	for _, pattern := range in.GetExcludePatterns() {
		p := string(pattern)
		if p == "" {
			return structerr.NewInvalidArgument("empty exclude pattern")
		}
		if strings.ContainsRune(p, 0) {
			return structerr.NewInvalidArgument("exclude pattern contains null byte: %q", p)
		}
		if !strings.HasPrefix(p, "refs/heads/") {
			return structerr.NewInvalidArgument("exclude pattern must start with %q: %q", "refs/heads/", p)
		}
	}

	if err := s.findLocalBranches(in, stream); err != nil {
		return err
	}

	return nil
}
```

**File:** internal/gitaly/service/ref/find_local_branches.go (L73-79)
```go
	opts := buildFindRefsOpts(ctx, in.GetPaginationParams())
	opts.sortBy = parseSortKey(in.GetSortBy())
	opts.cmdArgs = []gitcmd.Option{
		// %00 inserts the null character into the output (see for-each-ref docs)
		gitcmd.Flag{Name: "--format=" + strings.Join(format, "%00")},
		gitcmd.Flag{Name: "--sort=" + parseSortKey(in.GetSortBy())},
	}
```

**File:** internal/gitaly/service/ref/find_local_branches.go (L110-121)
```go
func parseSortKey(sortKey gitalypb.FindLocalBranchesRequest_SortBy) string {
	switch sortKey {
	case gitalypb.FindLocalBranchesRequest_NAME:
		return "refname"
	case gitalypb.FindLocalBranchesRequest_UPDATED_ASC:
		return "committerdate"
	case gitalypb.FindLocalBranchesRequest_UPDATED_DESC:
		return "-committerdate"
	}

	panic("never reached") // famous last words
}
```

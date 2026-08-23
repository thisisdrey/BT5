### Title
Unbounded per-item loop over client-supplied `ListCommitsByRefName`/`ListCommitsByOid` request fields causes single-request DoS of the RPC handler - (File: internal/gitaly/service/commit/list_commits_by_ref_name.go)

### Summary
`ListCommitsByRefName` and `ListCommitsByOid` accept an unbounded `repeated bytes`/`repeated string` field from the client and synchronously iterate over every entry in a single unary request, performing one `catfile.GetCommit` lookup per entry with no cap on the number of entries. This mirrors the "unbounded for loop over dynamically sized, attacker-controlled array" bug class from the referenced report: a caller who can reach these read-only RPCs can submit a request containing a very large number of short ref-name/OID entries and force the Gitaly worker to perform a correspondingly large number of sequential object lookups within one RPC call.

### Finding Description
`ListCommitsByRefName` validates only that the repository is set, then loops over `in.GetRefNames()` without any limit check, calling `catfile.GetCommit` for every element: [1](#0-0) 

The companion RPC `ListCommitsByOid` has the identical pattern, looping over `in.GetOid()` with no size limit, only recording a metric of the request size rather than enforcing a bound: [2](#0-1) 

Both `ref_names` and `oid` are declared as unbounded repeated fields in the proto with no documented or enforced maximum count: [3](#0-2) 

Unlike other RPCs in this service that cap work via `--max-count`/`Limit` flags passed to `git` (e.g. `CountCommits`, `ListCommits`), these two RPCs perform the iteration and lookups entirely in Go, once per entry, with no limit, no pagination and no early abort — this is precisely the "iterate over unbounded array" pattern flagged in the source report. Because the request is a single (non-streaming) unary message, the entire cost is incurred inside one RPC handler invocation before any response can be streamed back or before Gitaly's per-RPC concurrency limiter can meaningfully throttle it (the limiter only bounds the number of concurrent RPCs, not the amount of work inside one RPC): [4](#0-3) 

I found no `grpc.MaxRecvMsgSize` configuration in the codebase to cap the number of small entries that can be packed into a single request message, so this is bounded only by gRPC's default per-message size, which can still admit tens of thousands of short ref-name/OID entries.

### Impact Explanation
An ordinary, minimally-privileged client with read access to a repository (any actor who can call `CommitService`) can submit a single `ListCommitsByRefName` or `ListCommitsByOid` request containing a very large number of entries, forcing the Gitaly process to perform a correspondingly large number of sequential `catfile` lookups synchronously inside the RPC handler. This ties up a Gitaly worker/goroutine and the underlying `git-cat-file` process for an extended period, degrading service for that repository and, if repeated concurrently against different repositories, for the node as a whole — a resource-exhaustion/DoS of the RPC handler, one of the explicitly accepted impact categories.

### Likelihood Explanation
Likelihood is moderate-to-high: no special privilege beyond ordinary repository read access is required, the RPCs are simple unary read (`ACCESSOR`) calls, and there is no server-side validation capping the number of `ref_names`/`oid` entries. The main constraint is the default gRPC message size limit, which still permits a large number of short entries per request, and the attack can be trivially repeated/parallelized.

### Recommendation
- Add an explicit maximum-entries validation for `ref_names` (ListCommitsByRefName) and `oid` (ListCommitsByOid) in their request validators, rejecting requests exceeding a sane bound (mirroring the `Limit`/`MaxCount` patterns used elsewhere in the service).
- Alternatively/additionally, convert these RPCs to accept streamed requests (chunked) so that per-message size is bounded and processing can be pipelined and cancelled early, consistent with the guidance in `skills/gitaly-rpc-development/SKILL.md` about streaming unbounded repeated collections.
- Consider integrating this per-request item cost into Gitaly's existing concurrency/backpressure machinery so oversized requests are throttled or rejected before consuming git resources.

### Proof of Concept
1. As any client authorized to call Gitaly's `CommitService` for a target repository, construct a `ListCommitsByRefNameRequest` (or `ListCommitsByOidRequest`) with `ref_names` (or `oid`) populated with as many short unique entries (e.g. 4–8 bytes each) as fit under the default gRPC message size limit (tens of thousands of entries).
2. Send the request; observe the handler loop at [5](#0-4)  performing one `catfile.GetCommit` call per entry synchronously, with total RPC latency scaling linearly with the number of entries.
3. Repeat the request concurrently (optionally across multiple repositories) to tie up multiple Gitaly worker goroutines/`git-cat-file` processes simultaneously, degrading responsiveness for the node while each RPC remains under the concurrency-limiter's per-RPC concern (no single request violates the concurrency queue) but consumes disproportionate CPU/time per call.

### Citations

**File:** internal/gitaly/service/commit/list_commits_by_ref_name.go (L14-46)
```go
func (s *server) ListCommitsByRefName(in *gitalypb.ListCommitsByRefNameRequest, stream gitalypb.CommitService_ListCommitsByRefNameServer) error {
	ctx := stream.Context()
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	objectReader, cancel, err := s.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return structerr.NewInternal("%w", err)
	}
	defer cancel()

	sender := chunk.New(&commitsByRefNameSender{stream: stream})

	for _, refName := range in.GetRefNames() {
		commit, err := catfile.GetCommit(ctx, objectReader, git.Revision(refName))
		if errors.As(err, &catfile.NotFoundError{}) {
			continue
		}
		if err != nil {
			return structerr.NewInternal("%w", err)
		}

		commitByRef := &gitalypb.ListCommitsByRefNameResponse_CommitForRef{
			Commit: commit.GitCommit, RefName: refName,
		}

		if err := sender.Send(commitByRef); err != nil {
			return structerr.NewInternal("%w", err)
		}
	}
```

**File:** internal/gitaly/service/commit/list_commits_by_oid.go (L27-56)
```go
func (s *server) ListCommitsByOid(in *gitalypb.ListCommitsByOidRequest, stream gitalypb.CommitService_ListCommitsByOidServer) error {
	ctx := stream.Context()
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	objectReader, cancel, err := s.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return err
	}
	defer cancel()

	sender := chunk.New(&commitsByOidSender{stream: stream})
	listCommitsbyOidHistogram.Observe(float64(len(in.GetOid())))

	for _, oid := range in.GetOid() {
		commit, err := catfile.GetCommit(ctx, objectReader, git.Revision(oid))
		if errors.As(err, &catfile.NotFoundError{}) {
			continue
		}
		if err != nil {
			return err
		}

		if err := sender.Send(commit.GitCommit); err != nil {
			return err
		}
	}
```

**File:** proto/commit.proto (L590-612)
```text
// ListCommitsByOidRequest is the request for the ListCommitsByOid RPC.
message ListCommitsByOidRequest {
  // repository is the repository to list commits from.
  Repository repository = 1 [(target_repository)=true];
  // oid is a set of commitish object IDs to list commits for.
  // If there is no commit against a provided object ID, no error is thrown. It is simply ignored.
  repeated string oid = 2; // protolint:disable:this REPEATED_FIELD_NAMES_PLURALIZED
}

// ListCommitsByOidResponse is the response for the ListCommitsByOid RPC.
message ListCommitsByOidResponse {
  // commits are the list of commits for the provided object IDs from the request.
  repeated GitCommit commits = 1;
}

// ListCommitsByRefNameRequest is the request for the ListCommitsByRefName RPC.
message ListCommitsByRefNameRequest {
  // repository is the repository to list commits from.
  Repository repository = 1 [(target_repository)=true];
  // ref_names is a set of references to obtain commits for.
  // If there is no commit against a provided reference, no error is thrown. It is simply ignored.
  repeated bytes ref_names = 2;
}
```

**File:** doc/backpressure.md (L13-24)
```markdown
We employ concurrency limiting as our primary backpressure mechanism in Gitaly.

## Concurrency queue

Limit the number of concurrent RPCs that are in flight on each Gitaly node for each
repository per RPC using `[[concurrency]]` configuration:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
```
```

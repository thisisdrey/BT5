### Title
`FindChangedPaths` resolves every request/parent entry via unbounded git object lookups before enforcing any count limit - ([File: internal/gitaly/service/diff/find_changed_paths.go])

### Summary
`(s *server) validateFindChangedPathsRequestParams` iterates over every entry in `in.GetRequests()` (and the deprecated `in.GetCommits()`) and, for each one, spawns a `git-cat-file` object-info lookup via `objectInfoReader.Info()` to resolve the commit/tree revision — including a nested loop over every `ParentCommitRevisions` entry of a `CommitRequest` — with no upper bound on the number of requests or parent revisions before this expensive work is performed. [1](#0-0) [2](#0-1) 

### Finding Description
The `FindChangedPaths` RPC handler validates the request by calling `s.validateFindChangedPathsRequestParams`, which for `len(in.GetCommits())>0` first allocates `in.Requests` sized to the client-supplied `Commits` slice, and then, regardless of which path was taken, loops over `in.GetRequests()` performing a `resolveObjectWithType` call per `CommitRequest`/`TreeRequest`, plus one additional call per entry in `CommitRequest.GetParentCommitRevisions()`. [3](#0-2) [4](#0-3) 
Each `resolveObjectWithType` call issues a live `git-cat-file` info request against the repository's `catfile.ObjectInfoReader`. [5](#0-4) 
There is no check anywhere in `validateFindChangedPathsRequestParams` that bounds `len(in.GetRequests())` or the number of `ParentCommitRevisions` per request before this loop executes — unlike the `getBlobFromRequest()`/`validateBlobRequest()` pattern in the reference report, size/count validation for these fields never happens at all, so the resource-intensive work is done unconditionally for whatever the client sent. This directly mirrors the reported bug class: a caller-controlled repeated field drives an unbounded loop of per-item allocations/computation before any size or count check is applied.

### Impact Explanation
Because `FindChangedPaths` is a unary request handler reachable by any client with read access to a repository (an ordinary Gitaly RPC, not requiring elevated privilege), an attacker can submit a single request containing a very large `Requests` (or `Commits`) array, each with a large `ParentCommitRevisions` list, forcing Gitaly to spawn/query `git-cat-file` a large number of times purely for validation, before the RPC produces any diff output or returns an error. This consumes CPU and process/goroutine resources tied to the `catfile.ObjectInfoReader`/cache subsystem and can degrade or deny service to the Gitaly node handling the request, consistent with the "resource consumption DoS" class in the reference report.

### Likelihood Explanation
The `FindChangedPaths` RPC is part of the standard `DiffService` and is invoked in normal usage (e.g., viewing commit diffs), so any authenticated client capable of calling Gitaly RPCs against a repository can trigger this path with a single, protocol-valid request. The number of entries an attacker can pack in is only bounded by the gRPC request size limit; I could not find an explicit `MaxRecvMsgSize`/`MaxCallRecvMsgSize` configuration in this repo's search results, so it is uncertain whether a smaller custom limit is enforced elsewhere in the gRPC server setup — this should be verified before treating message-size as a mitigating control.

### Recommendation
Add explicit upper bounds in `validateFindChangedPathsRequestParams` (and/or the `.proto` definition) on `len(in.GetRequests())`/`len(in.GetCommits())` and on `len(CommitRequest.GetParentCommitRevisions())` per entry, rejecting oversized requests with `structerr.NewInvalidArgument` before any `resolveObjectWithType`/`objectInfoReader.Info` calls are made.

### Proof of Concept
Send a `FindChangedPathsRequest` with a `Requests` array containing many thousands of `CommitRequest` entries, each with a `ParentCommitRevisions` list also containing many entries (e.g. dummy short strings), sized up to the gRPC message limit. The handler will attempt to resolve every commit and every parent revision via `git-cat-file` before ever completing validation or returning `NotFound`/`InvalidArgument`, consuming CPU/catfile resources proportional to the crafted field sizes. [2](#0-1)

### Citations

**File:** internal/gitaly/service/diff/find_changed_paths.go (L362-381)
```go
func resolveObjectWithType(
	ctx context.Context,
	objectInfoReader catfile.ObjectInfoReader,
	revision string,
	expectedType string,
) (git.ObjectID, error) {
	if revision == "" {
		return "", structerr.NewInvalidArgument("revision cannot be empty")
	}

	info, err := objectInfoReader.Info(ctx, git.Revision(fmt.Sprintf("%s^{%s}", revision, expectedType)))
	if err != nil {
		if errors.As(err, &catfile.NotFoundError{}) {
			return "", structerr.NewNotFound("revision can not be found: %q", revision)
		}
		return "", err
	}

	return info.Oid, nil
}
```

**File:** internal/gitaly/service/diff/find_changed_paths.go (L383-412)
```go
func (s *server) validateFindChangedPathsRequestParams(ctx context.Context, in *gitalypb.FindChangedPathsRequest) error {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	gitRepo := s.localRepoFactory.Build(repository)

	if len(in.GetCommits()) > 0 { //nolint:staticcheck
		if len(in.GetRequests()) > 0 {
			return structerr.NewInvalidArgument("cannot specify both commits and requests")
		}

		in.Requests = make([]*gitalypb.FindChangedPathsRequest_Request, len(in.GetCommits())) //nolint:staticcheck
		for i, commit := range in.GetCommits() {                                              //nolint:staticcheck
			in.Requests[i] = &gitalypb.FindChangedPathsRequest_Request{
				Type: &gitalypb.FindChangedPathsRequest_Request_CommitRequest_{
					CommitRequest: &gitalypb.FindChangedPathsRequest_Request_CommitRequest{
						CommitRevision: commit,
					},
				},
			}
		}
	}

	objectInfoReader, cancel, err := s.catfileCache.ObjectInfoReader(ctx, gitRepo)
	if err != nil {
		return structerr.NewInternal("getting object info reader: %w", err)
	}
	defer cancel()
```

**File:** internal/gitaly/service/diff/find_changed_paths.go (L414-439)
```go
	for _, request := range in.GetRequests() {
		switch t := request.GetType().(type) {
		case *gitalypb.FindChangedPathsRequest_Request_CommitRequest_:
			oid, err := resolveObjectWithType(
				ctx,
				objectInfoReader,
				t.CommitRequest.GetCommitRevision(),
				"commit",
			)
			if err != nil {
				return structerr.NewInternal("resolving commit: %w", err)
			}
			t.CommitRequest.CommitRevision = oid.String()

			for i, commit := range t.CommitRequest.GetParentCommitRevisions() {
				oid, err := resolveObjectWithType(
					ctx,
					objectInfoReader,
					commit,
					"commit",
				)
				if err != nil {
					return structerr.NewInternal("resolving commit parent: %w", err)
				}
				t.CommitRequest.ParentCommitRevisions[i] = oid.String()
			}
```

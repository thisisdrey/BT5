## Analysis

The Sherlock report's root cause is that unprivileged, attacker-supplied WASI import calls execute with **no cost accounting**, so a single crafted call (looped inside a user-supplied WASM program) can consume unbounded CPU without the concurrency/gas system ever seeing it as "expensive." The closest reachable analog in Gitaly is the RPC concurrency-limiting subsystem, which Gitaly's own documentation states treats **every request as equally expensive regardless of actual resource cost**, combined with handlers that iterate over an attacker-controlled, protobuf `repeated` field with **no per-request item cap**, so a single admitted RPC call can perform unbounded backend work while occupying only one limiter slot.

### Title
Single-slot concurrency limiting lets a single crafted RPC (e.g. `ListCommitsByOid`/`GetBlobs`) consume unbounded CPU/IO without any per-request cost accounting - ([File: internal/gitaly/service/commit/list_commits_by_oid.go])

### Summary
Gitaly's concurrency limiter is Gitaly's only defense against resource exhaustion (`doc/backpressure.md`), and it caps the number of *concurrent* in-flight RPCs per repository/RPC method. It does not meter or bound the amount of work performed *inside* a single admitted call. Gitaly's own architecture notes explicitly acknowledge this gap: "The concurrency limiter today treats every request as equally expensive" [1](#0-0) . Handlers such as `ListCommitsByOid` and `GetBlobs` loop over a client-supplied `repeated` field with no cap on the number of elements, performing one backend Git object lookup per element [2](#0-1) [3](#0-2) . This is the structural equivalent of the WASI "unmetered execution" bug: a single admitted unit of work (one gRPC call / one WASI import call) is billed as constant cost by the outer control mechanism while its actual internal work is attacker-controlled and effectively unbounded.

### Finding Description
- The `ConcurrencyLimiter.Limit` function only gates admission of a call as a whole; once `f()` (the handler) is invoked, there is no metering of the work performed inside it [4](#0-3) .
- `ListCommitsByOid` iterates over `in.GetOid()`, a `repeated string` field fully controlled by the client, invoking `catfile.GetCommit` (a Git object lookup, involving a subprocess/`git cat-file` round-trip) for every entry with no cap on the number of OIDs processed per call [5](#0-4) . The code even records the request size in a histogram, evidencing awareness that request size varies widely, yet applies no limit [6](#0-5) .
- `GetBlobs` similarly loops over `req.GetRevisionPaths()`, an attacker-controlled `repeated` field, performing a tree-entry lookup plus object read per path with no bound on the number of paths [3](#0-2) [7](#0-6) .
- No `grpc.MaxRecvMsgSize`/`MaxCallRecvMsgSize` server option is configured in the codebase, so the effective request-size ceiling is the gRPC-go library default, which still permits requests containing tens of thousands of short OID/path strings.
- Because the concurrency limiter admits the call the moment it acquires one semaphore slot (`sem.acquire`), and that slot is held for the call's *entire* duration regardless of how many internal Git object lookups it triggers [8](#0-7) , a single call with a large-but-message-size-legal `repeated` field can hold a limiter slot for a disproportionately long time while driving disproportionate CPU/subprocess/disk-IO load — exactly the "one unmetered unit of execution, unboundedly repeated internally" pattern from the source report, just expressed as "one repeated-field element, unboundedly repeated" instead of "one WASI syscall, unboundedly looped."

### Impact Explanation
An unauthenticated-but-authorized (or even just rate-unlimited, since Gitaly removed rate limiting per `doc/backpressure.md`) client can submit a small number of high-cardinality `ListCommitsByOid`/`GetBlobs` requests, each of which occupies only one concurrency-limiter slot yet drives a large multiple of backend Git subprocess/catfile work. Because the limiter's admission decision doesn't reflect the request's true cost, an attacker can degrade a target repository's Gitaly node performance (CPU, subprocess, and I/O exhaustion) without ever exceeding configured concurrency thresholds, causing delayed or failed Git operations for legitimate users of that repository/storage — a DoS of the handler, analogous to the WASI report's "delay block building, possibly chain halt" impact translated to Gitaly's "delay Git operations, possible node overload" outcome.

### Likelihood Explanation
Any authenticated Gitaly client (which in GitLab's architecture includes any user who can reach Gitaly through gitlab-shell/Workhorse/Rails for an accessible repository) can construct such a request; no special privilege beyond ordinary repository read access is required, and the `repeated` fields are ordinary, documented protobuf inputs with no server-side count validation. This makes the likelihood high for any deployment relying on Gitaly's stated backpressure model as its DoS mitigation.

### Recommendation
Introduce a maximum element count (and/or a computed cost estimate) for attacker-controlled `repeated` fields such as `ListCommitsByOidRequest.oid` and `GetBlobsRequest.revision_paths`, rejecting requests that exceed the bound with `INVALID_ARGUMENT`/`RESOURCE_EXHAUSTED`, similar to the pattern the cost-aware admission layer proposal in `doc/load-management-architecture.md` describes. More generally, extend the concurrency limiter (or a new cost-aware admission layer) to account for the actual amount of internal work a request will perform rather than treating every admitted call as a single, equally-costed unit.

### Proof of Concept
1. As any client authorized to call `CommitService`/`BlobService` RPCs against a target repository, build a `ListCommitsByOidRequest` (or `GetBlobsRequest`) whose `oid`/`revision_paths` field contains tens of thousands of valid-looking OIDs/paths, kept just under the gRPC message-size limit.
2. Send this single RPC call. It is admitted by the concurrency limiter as one unit (one semaphore token), per `ConcurrencyLimiter.Limit` [4](#0-3) .
3. Internally, the handler performs one `catfile.GetCommit`/tree lookup per element with no cap [2](#0-1) , consuming CPU and subprocess resources far exceeding what the limiter's per-slot accounting assumes.
4. Repeating this with a handful of concurrent such requests (still within the configured `max_per_repo`/`max_queue_size`) can saturate the node's actual CPU/IO capacity while appearing "compliant" to the limiter, degrading service for other repositories/tenants on the same Gitaly node.

### Citations

**File:** doc/load-management-architecture.md (L197-205)
```markdown
The concurrency limiter today treats every request as equally expensive. In
practice, requests served from cache consume minimal resources (disk streaming)
while cache misses run full Git subprocess pipelines. Rather than feeding the
limiter a cost hint (which still consumes a limiter slot), cache-hit requests
skip the limiter entirely — keeping the concurrency limiter pure and ensuring
limits designed for expensive operations do not penalize cheap ones.

This separation also keeps the concurrency limiter's responsibility narrow: every
request that reaches it is treated as expensive.
```

**File:** internal/gitaly/service/commit/list_commits_by_oid.go (L16-56)
```go
var listCommitsbyOidHistogram = promauto.NewHistogram(
	prometheus.HistogramOpts{
		Name: "gitaly_list_commits_by_oid_request_size",
		Help: "Number of commits requested in a ListCommitsByOid request",

		// We want to count the pathological case where the request is empty. I
		// am not sure if with floats, Observe(0) would go into bucket 0. Use
		// bucket 0.001 because 0 <= 0.001 for sure.
		Buckets: []float64{0.001, 1, 5, 10, 20},
	})

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

**File:** internal/gitaly/service/blob/get_blobs.go (L33-94)
```go
	for _, revisionPath := range req.GetRevisionPaths() {
		revision := revisionPath.GetRevision()
		path := revisionPath.GetPath()

		if len(path) > 1 {
			path = bytes.TrimRight(path, "/")
		}

		treeEntry, err := tef.FindByRevisionAndPath(ctx, revision, string(path))
		if err != nil {
			return structerr.NewInternal("find by revision and path: %w", err)
		}

		response := &gitalypb.GetBlobsResponse{Revision: revision, Path: path}

		if treeEntry == nil || len(treeEntry.GetOid()) == 0 {
			if err := stream.Send(response); err != nil {
				return structerr.NewInternal("send: %w", err)
			}

			continue
		}

		response.Mode = treeEntry.GetMode()
		response.Oid = treeEntry.GetOid()

		if treeEntry.GetType() == gitalypb.TreeEntry_COMMIT {
			response.IsSubmodule = true
			response.Type = gitalypb.ObjectType_COMMIT

			if err := stream.Send(response); err != nil {
				return structerr.NewInternal("send: %w", err)
			}

			continue
		}

		objectInfo, err := objectInfoReader.Info(ctx, git.Revision(treeEntry.GetOid()))
		if err != nil {
			return structerr.NewInternal("read object info: %w", err)
		}

		response.Size = objectInfo.Size

		var ok bool
		response.Type, ok = treeEntryToObjectType[treeEntry.GetType()]

		if !ok {
			continue
		}

		if response.GetType() != gitalypb.ObjectType_BLOB {
			if err := stream.Send(response); err != nil {
				return structerr.NewInternal("send: %w", err)
			}
			continue
		}

		if err = sendBlobTreeEntry(response, stream, objectReader, req.GetLimit()); err != nil {
			return err
		}
	}
```

**File:** internal/limiter/concurrency_limiter.go (L197-241)
```go
func (c *ConcurrencyLimiter) Limit(ctx context.Context, limitingKey string, f LimitedFunc) (interface{}, error) {
	span, ctx := tracing.StartSpanIfHasParent(
		ctx,
		"limiter.ConcurrencyLimiter.Limit",
		[]attribute.KeyValue{
			attribute.String("key", limitingKey),
		},
	)
	defer span.End()

	if c.currentLimit() <= 0 {
		return f()
	}

	sem := c.getConcurrencyLimit(limitingKey)
	defer c.putConcurrencyLimit(limitingKey)

	start := time.Now()

	if err := sem.acquire(ctx, limitingKey); err != nil {
		queueTime := time.Since(start)
		switch {
		case errors.Is(err, ErrMaxQueueSize):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_size")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueSize).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		case errors.Is(err, ErrMaxQueueTime):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_time")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueTime).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		default:
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "other")
			return nil, fmt.Errorf("unexpected error when dequeueing request: %w", err)
		}
	}
	defer sem.release()

	c.monitor.Enter(ctx, sem.inProgress(), time.Since(start))
	defer c.monitor.Exit(ctx)
	return f()
}
```

**File:** proto/blob.proto (L103-121)
```text
// GetBlobsRequest is a request for the GetBlobs RPC.
message GetBlobsRequest {
  // RevisionPath is a combination of revision and path. All objects reachable in the subdirectory of the treeish
  // will be returned.
  message RevisionPath {
    // revision is the revision that identifies the tree-ish. Must not be empty.
    string revision = 1;
    // path is the path relative to the treeish revision that shall be searched for a blob. If the path is empty the
    // root directory of the tree-ish will be searched.
    bytes path = 2;
  }

  // repository is the repository that shall be searched.
  Repository repository = 1[(target_repository)=true];
  // revision_paths identifies the set of revision/path pairs that shall be searched for blobs.
  repeated RevisionPath revision_paths = 2;
  // limit is the maximum number of bytes we want to receive. Use '-1' to get the full blobs no matter how big.
  int64 limit = 3;
}
```

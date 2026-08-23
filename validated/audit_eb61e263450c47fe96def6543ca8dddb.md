### Title
Unbounded per-request work in `GetBlobs` RPC allows single-request resource-exhaustion DoS bypassing Gitaly's concurrency-based backpressure - ([File: internal/gitaly/service/blob/get_blobs.go])

### Summary
Gitaly's documented and implemented DoS mitigation (`internal/grpc/middleware/limithandler`, `internal/limiter`, `doc/backpressure.md`) protects against too many *concurrent* RPCs to the same repository/RPC pair, but does nothing to bound the amount of work a *single* RPC call can trigger. The `GetBlobs` RPC accepts an unbounded `repeated RevisionPath revision_paths` field with no cap on element count, and its handler loops synchronously over every element performing a Git object/tree lookup (`FindByRevisionAndPath`) and object-info read for each one. A single crafted request with a very large number of `revision_paths` entries can force Gitaly to perform an arbitrarily large number of Git subprocess/catfile operations within one RPC call, consuming CPU and catfile-cache resources without ever triggering the per-RPC concurrency limiter (since only one RPC is "in flight").

### Finding Description
`GetBlobsRequest` is defined with an unrestricted `repeated RevisionPath revision_paths` field and no maximum item count: [1](#0-0) 

The validation function `validateGetBlobsRequest` only checks that the list is non-empty and that each revision string is a syntactically valid revision — it never caps `len(req.GetRevisionPaths())`: [2](#0-1) 

The handler then iterates over every entry unconditionally, performing a tree-entry lookup and, for blobs, a full object-info read and streamed read for each one: [3](#0-2) 

By contrast, sibling RPCs in the same service (`ListBlobs`, `ListAllBlobs`, `ListLFSPointers`) explicitly define a `limit`/`bytes_limit` field to bound server-side work per call: [4](#0-3) 

Gitaly's only DoS defense mechanism is the concurrency limiter middleware, which throttles the number of *simultaneous* RPCs per RPC-name/repository key — it does not inspect or bound the cost of an individual request: [5](#0-4) [6](#0-5) 

The project's own documentation explicitly confirms that dedicated rate limiting was intentionally removed from Gitaly in favor of only this concurrency-queue mechanism: [7](#0-6) 

Because a gRPC message can carry a large number of small `RevisionPath` entries (each just a revision string and short path) within the default message-size limits, a single `GetBlobs` call is sufficient to enqueue a very large number of catfile/tree lookups server-side, entirely within one "in-flight" RPC slot that the concurrency limiter treats as a single unit of work.

### Impact Explanation
An authenticated caller able to issue `GetBlobs` RPCs (e.g., via GitLab Rails/Workhorse relaying a user-triggered API call, or any client with a valid Gitaly token) can submit one request with a very large `revision_paths` list, causing Gitaly to perform a correspondingly large number of Git object/tree lookups and catfile reads sequentially in a single RPC handler invocation. This can consume significant CPU and I/O on the Gitaly node while only occupying a single concurrency-limiter slot, allowing the attacker to degrade or exhaust node resources with comparatively few actual "requests," directly mirroring the reported "lack of resources and rate limiting" bug class — the existing concurrency-based backpressure does not account for per-request cost.

### Likelihood Explanation
Likelihood is moderate: it requires the ability to invoke `GetBlobs` with attacker-influenced `revision_paths`, which is plausible for any workflow that lets a user request a large batch of file paths at a given ref (e.g., diff/blob viewers, LFS-adjacent flows) without the calling application enforcing its own batch-size cap. No authentication bypass or privileged access is required beyond whatever access is already needed to call the RPC through the normal Gitaly client path.

### Recommendation
- Add and enforce an explicit maximum on `len(RevisionPaths)` (and similarly for other unbounded `repeated` request fields across RPCs lacking a `limit`) in `validateGetBlobsRequest`, rejecting oversized requests with `InvalidArgument`.
- Consider adding a per-request "cost" accounting hook to the concurrency limiter (as hinted at in `doc/load-management-architecture.md`'s discussion of cost-aware admission) so that RPCs with attacker-controlled batch sizes contribute to backpressure decisions proportionally to their cost, not just as one unit of concurrency.
- Audit other RPCs with unbounded `repeated` fields for the same class of issue and apply consistent caps, following the `limit`/`bytes_limit` pattern already used by `ListBlobs`/`ListAllBlobs`/`ListLFSPointers`.

### Proof of Concept
Not independently executed (index-only analysis); the code path reachable by any client is:
1. Client issues `BlobService/GetBlobs` with a single valid `Repository` and a `revision_paths` array containing a very large number of `RevisionPath` entries (e.g., tens of thousands of `{revision: <valid-oid>, path: <short-path>}` pairs), staying under the default gRPC message size limit.
2. `GetBlobs` (`internal/gitaly/service/blob/get_blobs.go:157-176`) calls `sendGetBlobsResponse`, which loops over every `RevisionPath` and performs a `FindByRevisionAndPath` + `ObjectInfoReader.Info` call per entry with no early termination or count limit.
3. The single RPC call thus drives a number of Git catfile operations proportional to the attacker-chosen list size, while the concurrency limiter (if configured for this RPC) only accounts for it as one "in-progress" call. [8](#0-7)

### Citations

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

**File:** proto/blob.proto (L152-172)
```text
// ListBlobsRequest is a request for the ListBlobs RPC.
message ListBlobsRequest {
  // repository is the repository in which blobs should be enumerated.
  Repository repository = 1 [(target_repository)=true];
  // revisions is the list of revisions to retrieve blobs from. These revisions
  // will be walked. Supports pseudo-revisions `--all` and `--not` as well as
  // negated revisions via `^revision`. Revisions cannot start with a leading
  // dash. Please consult gitrevisions(7) for more info. Must not be empty.
  repeated string revisions = 2;
  // limit is the maximum number of blobs to return. If set to its default
  // (`0`), then all found blobs will be returned.
  uint32 limit = 3;
  // bytes_limit is the maximum number of bytes to receive for each blob. If set
  // to `0`, then no blob data will be sent. If `-1`, then all blob data will
  // be sent without any limits.
  int64 bytes_limit = 4;
  // with_paths determines whether paths of blobs should be returned. When
  // set to `true`, paths are returned on a best-effort basis: a path will only
  // exist if the blob was traversed via a tree.
  bool with_paths = 5;
}
```

**File:** internal/gitaly/service/blob/get_blobs.go (L23-94)
```go
func sendGetBlobsResponse(
	req *gitalypb.GetBlobsRequest,
	stream gitalypb.BlobService_GetBlobsServer,
	objectReader catfile.ObjectContentReader,
	objectInfoReader catfile.ObjectInfoReader,
) error {
	ctx := stream.Context()

	tef := catfile.NewTreeEntryFinder(objectReader)

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

**File:** internal/gitaly/service/blob/get_blobs.go (L157-176)
```go
func (s *server) GetBlobs(req *gitalypb.GetBlobsRequest, stream gitalypb.BlobService_GetBlobsServer) error {
	if err := validateGetBlobsRequest(stream.Context(), s.locator, req); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	repo := s.localRepoFactory.Build(req.GetRepository())

	objectReader, cancel, err := s.catfileCache.ObjectReader(stream.Context(), repo)
	if err != nil {
		return structerr.NewInternal("creating object reader: %w", err)
	}
	defer cancel()

	objectInfoReader, cancel, err := s.catfileCache.ObjectInfoReader(stream.Context(), repo)
	if err != nil {
		return structerr.NewInternal("creating object info reader: %w", err)
	}
	defer cancel()

	return sendGetBlobsResponse(req, stream, objectReader, objectInfoReader)
```

**File:** internal/gitaly/service/blob/get_blobs.go (L179-195)
```go
func validateGetBlobsRequest(ctx context.Context, locator storage.Locator, req *gitalypb.GetBlobsRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetRevisionPaths()) == 0 {
		return errors.New("empty RevisionPaths")
	}

	for _, rp := range req.GetRevisionPaths() {
		if err := git.ValidateRevision([]byte(rp.GetRevision())); err != nil {
			return err
		}
	}

	return nil
}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L74-98)
```go
// UnaryInterceptor returns a Unary Interceptor
func (c *LimiterMiddleware) UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		lockKey := c.getLockKey(ctx)
		if lockKey == "" {
			return handler(ctx, req)
		}

		// Check if request is authenticated
		limiter := c.methodLimiters[info.FullMethod]
		unauthLimiter, ok := c.methodLimitersUnauthenticated[info.FullMethod]
		if !auth.IsAuthenticated(ctx) && ok {
			limiter = unauthLimiter
		}

		if limiter == nil {
			// No concurrency limiting
			return handler(ctx, req)
		}

		return limiter.Limit(ctx, lockKey, func() (interface{}, error) {
			return handler(ctx, req)
		})
	}
}
```

**File:** doc/backpressure.md (L1-24)
```markdown
# Request limiting in Gitaly

In the GitLab ecosystem, Gitaly is the service that is at the bottom of the
stack for Git data access. This means that when there is a surge of
requests to retrieve or change a piece of Git data, the I/O happens in Gitaly.
This can lead to Gitaly being overwhelmed due to system resource exhaustion
because all Git access goes through Gitaly.

If there is a surge of traffic beyond what Gitaly can handle, Gitaly should
be able to push back on the client calling. Gitaly shouldn't subserviently agree
to process more than it can handle.

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

**File:** doc/backpressure.md (L54-56)
```markdown
## Note on Rate Limiting

Rate limiting has been removed from Gitaly. For more information about why and the alternatives, please see [issue #5011](https://gitlab.com/gitlab-org/gitaly/-/issues/5011).
```

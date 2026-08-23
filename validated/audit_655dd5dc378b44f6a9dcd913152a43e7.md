### Title
Unbounded `GetBlobsRequest.revision_paths` list allows any client to force unlimited per-item Git object lookups, causing RPC-handler resource-exhaustion DoS - (File: `internal/gitaly/service/blob/get_blobs.go`)

### Summary
The externally reported bug is a griefing vector where a second, unprivileged party can invoke an operation on behalf of a user with a request array whose granularity is entirely attacker-controlled, forcing the target to pay for many small, expensive units of work instead of one large cheap one. The core failure is the absence of any limit on the size/shape of a caller-supplied repeated field that directly drives a per-element expensive operation.

### Finding Description
`BlobService.GetBlobs` accepts a `GetBlobsRequest` whose `revision_paths` field is a `repeated RevisionPath` list fully controlled by the caller [1](#0-0) . The only validation performed is that the list is non-empty and that each revision string is syntactically valid — there is no upper bound on the number of entries: [2](#0-1) 

`sendGetBlobsResponse` then iterates over every single `revisionPath` and, for each one, performs a tree lookup via `tef.FindByRevisionAndPath` and an `objectInfoReader.Info` call (each of which talks to a long-lived `git cat-file` process), followed by streaming blob data: [3](#0-2) [4](#0-3) 

Because there is no limit on `len(revision_paths)`, and gRPC message-size limits are the only practical bound, a single request can contain many thousands of `RevisionPath` entries within a single message (or even more when combined with streaming), each of which drives a full tree/object walk. This is structurally identical to the reported bug class: the caller (who may not even be the resource owner in the RPC's broader authorization model — Gitaly's RPCs are invoked on behalf of GitLab users through Rails/Workhorse) controls how finely a bulk operation is fragmented, and Gitaly performs the resulting work unconditionally, with no accounting for total requested work versus a single equivalent bulk call. `GetLFSPointersRequest.blob_ids` has the same unbounded-list pattern feeding a similar per-ID catfile pipeline [5](#0-4) .

### Impact Explanation
An ordinary, otherwise-authorized caller of `GetBlobs`/`GetLFSPointers` can submit a single request with an arbitrarily large `revision_paths`/`blob_ids` list, forcing Gitaly to perform a correspondingly large number of tree/object lookups against `git cat-file` subprocesses in one RPC invocation. Because Gitaly's concurrency/backpressure controls are scoped at the RPC/repository level rather than at the granularity of "amount of work requested inside one message" [6](#0-5) , a single such request can consume disproportionate CPU and catfile-session resources on the Gitaly node serving that repository, degrading service for all other tenants/repositories sharing that node — the direct analog of the reported griefing-via-fragmentation impact.

### Likelihood Explanation
Both RPCs are reachable through GitLab's normal blob-browsing/LFS code paths (e.g., "get raw files across multiple paths/revisions"), and the request shape (`revision_paths`, `blob_ids`) is trivially expandable by any client capable of calling these RPCs, with no server-side cap to reject an oversized list before work begins.

### Recommendation
Enforce an explicit maximum length on `GetBlobsRequest.revision_paths` and `GetLFSPointersRequest.blob_ids` (and similarly for other unbounded repeated fields feeding per-item Git subprocess work), rejecting oversized requests with `InvalidArgument` in `validateGetBlobsRequest`/`validateGetLFSPointersRequest` before any per-item processing begins, and/or account for per-request item count in the existing concurrency/backpressure limiting so bulk requests cannot bypass the coarse-grained RPC concurrency limits.

### Proof of Concept
1. Call `BlobService.GetBlobs` against any accessible repository with `revision_paths` populated with N (e.g., 100,000) distinct valid `RevisionPath` entries pointing at existing blobs.
2. Observe that `validateGetBlobsRequest` accepts the request unconditionally (only checks non-empty and per-entry revision syntax) [7](#0-6) .
3. Observe `sendGetBlobsResponse` performing N sequential tree/object lookups and catfile info reads in a single RPC invocation with no per-request cap [8](#0-7) , consuming CPU/catfile resources proportional to attacker-chosen N rather than to any legitimate bound.

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

**File:** internal/gitaly/service/blob/lfs_pointers.go (L125-160)
```go
// GetLFSPointers takes the list of requested blob IDs and filters them down to blobs which are
// valid LFS pointers. It is fine to pass blob IDs which do not point to a valid LFS pointer, but
// passing blob IDs which do not exist results in an error.
func (s *server) GetLFSPointers(req *gitalypb.GetLFSPointersRequest, stream gitalypb.BlobService_GetLFSPointersServer) error {
	ctx := stream.Context()

	if err := validateGetLFSPointersRequest(stream.Context(), s.locator, req); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	repo := s.localRepoFactory.Build(req.GetRepository())

	chunker := chunk.New(&lfsPointerSender{
		send: func(pointers []*gitalypb.LFSPointer) error {
			return stream.Send(&gitalypb.GetLFSPointersResponse{
				LfsPointers: pointers,
			})
		},
	})

	objectInfoReader, cancel, err := s.catfileCache.ObjectInfoReader(ctx, repo)
	if err != nil {
		return structerr.NewInternal("creating object info reader: %w", err)
	}
	defer cancel()

	objectReader, cancel, err := s.catfileCache.ObjectReader(ctx, repo)
	if err != nil {
		return structerr.NewInternal("creating object reader: %w", err)
	}
	defer cancel()

	blobs := make([]gitpipe.RevisionResult, len(req.GetBlobIds()))
	for i, blobID := range req.GetBlobIds() {
		blobs[i] = gitpipe.RevisionResult{OID: git.ObjectID(blobID)}
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

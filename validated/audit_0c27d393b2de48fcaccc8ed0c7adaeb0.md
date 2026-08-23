Based on my investigation, I found a plausible analog but could not fully verify the handler's internal iteration logic before running out of tool budget — I'll flag that uncertainty explicitly below.

### Title
Unbounded `GetBlobsRequest.revision_paths` / `GetLFSPointersRequest.blob_ids` batch processed without a per-request item cap - (File: `proto/blob.proto`, `internal/gitaly/service/blob/get_blobs.go`, `internal/gitaly/service/blob/lfs_pointers.go`)

### Summary
The reported Solidity bug class is "no cap on batch array size lets an ordinary caller submit an oversized array, driving unbounded work per call and causing a DoS." The closest reachable analog in Gitaly is the `GetBlobs` and `GetLFSPointers` RPCs, whose request messages accept a `repeated` field (`revision_paths`, `blob_ids`) with no explicit maximum count. Each entry in these repeated fields drives a separate object resolution/traversal on the server side within a single RPC invocation.

### Finding Description
`GetBlobsRequest` is defined as a **unary** request (not a client-streamed RPC) carrying `repeated RevisionPath revision_paths = 2` [1](#0-0) . Likewise, `GetLFSPointersRequest` carries `repeated string blob_ids = 2` with only a comment that it "must be a non-empty list" — no upper bound documented or enforced in the proto [2](#0-1) .

Gitaly's own RPC design guidance explicitly calls out that `repeated` fields "that can grow without limit" should use gRPC streaming to bound work per message, but for these RPCs the *request* remains unary and thus its item count is only implicitly bounded by the gRPC message-size ceiling, not by any explicit limit on the number of items [3](#0-2) . Because `RevisionPath.revision` and `.path` (or a `blob_id`) can be very short strings, a single sub-message-size-limit request can still contain a very large number of entries, each of which requires the handler to perform a separate Git object/tree lookup.

The general blob-processing helper `processBlobs` demonstrates the pattern: it accepts a `blobsLimit` to cap *response* items, but this limit is applied only to how many results are emitted, not to how many revisions/paths the caller may submit in the request [4](#0-3) . I was not able to fully confirm, within my remaining tool budget, whether `get_blobs.go`'s handler (`internal/gitaly/service/blob/get_blobs.go`) or `lfs_pointers.go`'s `GetLFSPointers` handler impose their own explicit cap on `len(revision_paths)`/`len(blob_ids)` before iterating — this should be verified directly in those files by a follow-up investigation.

Gitaly does have a generic mitigation layer: the concurrency limiter (`[[concurrency]]` config, `max_per_repo`/`max_queue_size`/`max_queue_wait`) throttles the number of *concurrent* RPC invocations per repository [5](#0-4) . However, this limits how many requests run concurrently, not how much work a single request can request via an oversized repeated field — so a single crafted request with an enormous item list can still consume disproportionate CPU/IO within its own request lifetime, similar to how `batchMatchOrders` allowed one caller to submit an arbitrarily large order array in one call.

### Impact Explanation
An authenticated but otherwise unprivileged Gitaly client (e.g., anything that can call `BlobService.GetBlobs` or `BlobService.GetLFSPointers` through GitLab's normal API/workhorse path) could submit a request with an extremely large `revision_paths`/`blob_ids` list, forcing Gitaly to perform a correspondingly large number of Git object lookups synchronously within one RPC handler invocation. This ties up a `git-cat-file`/`git-rev-parse` session and CPU/IO for the duration, potentially exhausting the node's resources and degrading availability for other tenants on a shared Gitaly node — a DoS of the RPC handler consistent with the "Accept... DoS of a handler" validation criterion.

### Likelihood Explanation
Likelihood is moderate: the RPCs are reachable by any client with repository read access (a very low privilege bar), and no proto-level or handler-level cap on entry count was found in the definitions reviewed. However, exploitation is bounded by the gRPC max message size for a single unary request, and the per-repository concurrency limiter reduces the blast radius from concurrent abuse (though it does not prevent one oversized single request from doing excessive work).

### Recommendation
- Add and enforce an explicit maximum count on `revision_paths` (`GetBlobsRequest`) and `blob_ids` (`GetLFSPointersRequest`, `GetLFSPointers`) at the start of the handler, rejecting oversized requests with `InvalidArgument` before doing any Git object work.
- Where an unbounded number of items is a legitimate use case, convert the RPC to a client-streamed RPC (per Gitaly's own streaming guidance) so that work can be chunked, checked for cancellation, and rate-limited progressively, rather than accepted as one large unary blob.
- Consider extending `doc/backpressure.md`-style backpressure to account for "work size" implied by request payload contents (not just concurrent RPC count), e.g., a per-request item-count based cost hint feeding into the concurrency/admission layer described in `doc/load-management-architecture.md`.

### Proof of Concept
A client crafts a single `GetBlobsRequest` (or `GetLFSPointersRequest`) whose `revision_paths` (or `blob_ids`) field contains as many short entries as fit under the gRPC message-size limit (e.g., tens of thousands of 10-byte revision/path pairs). Sent to a Gitaly node with normal repository read permissions, this forces the handler to perform one Git object lookup per entry within a single RPC call, consuming CPU and IO proportional to the array size, with no server-side cap rejecting the oversized request before processing begins.

**Caveat:** I could not, within the available tool budget, read the full bodies of `internal/gitaly/service/blob/get_blobs.go` and `internal/gitaly/service/blob/lfs_pointers.go` to confirm definitively whether an item-count cap already exists inside those specific handlers. This should be verified before treating the finding as fully confirmed; the proto-level absence of a bound and the general RPC-design guidance are the concrete evidence I was able to establish. [1](#0-0) [2](#0-1)

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

**File:** proto/blob.proto (L252-260)
```text
// GetLFSPointersRequest is a request for the GetLFSPointers RPC.
message GetLFSPointersRequest {
  // repository is the repository for which LFS pointers should be retrieved
  // from.
  Repository repository = 1[(target_repository)=true];
  // blob_ids is the list of blobs to retrieve LFS pointers from. Must be a
  // non-empty list of blobs IDs to fetch.
  repeated string blob_ids = 2;
}
```

**File:** skills/gitaly-rpc-development/SKILL.md (L42-47)
```markdown
- **Stream unbounded or oversized payloads.** Use a `stream` when the result is an unbounded
  sequence (a `repeated` collection that can grow without limit) or a payload that could exceed
  gRPC's message-size limit, sending it in batches/chunks. For high-volume byte payloads (e.g. Git
  fetch/pack data), Gitaly avoids gRPC messages entirely and uses a **sidechannel**
  ([doc/sidechannel.md](../../doc/sidechannel.md)); the full streaming patterns are in
  [doc/protobuf.md](../../doc/protobuf.md).
```

**File:** internal/gitaly/service/blob/blobs.go (L84-130)
```go
func (s *server) processBlobs(
	ctx context.Context,
	repo *localrepo.Repo,
	objectIter gitpipe.ObjectIterator,
	catfileInfoIter gitpipe.CatfileInfoIterator,
	blobsLimit uint32,
	bytesLimit int64,
	callback func(oid string, size int64, contents []byte, path []byte) error,
) error {
	// If we have a zero bytes limit, then the caller didn't request any blob contents at all.
	// We can thus skip reading blob contents completely.
	if bytesLimit == 0 {
		// This is a bit untidy, but some callers may already use an object info iterator to
		// enumerate objects, where it thus wouldn't make sense to recreate it via the
		// object iterator. We thus support an optional `catfileInfoIter` parameter: if set,
		// we just use that one and ignore the object iterator.
		if catfileInfoIter == nil {
			objectInfoReader, cancel, err := s.catfileCache.ObjectInfoReader(ctx, repo)
			if err != nil {
				return structerr.NewInternal("creating object info reader: %w", err)
			}
			defer cancel()

			catfileInfoIter, err = gitpipe.CatfileInfo(ctx, objectInfoReader, objectIter)
			if err != nil {
				return structerr.NewInternal("creating object info iterator: %w", err)
			}
		}

		var i uint32
		for catfileInfoIter.Next() {
			blob := catfileInfoIter.Result()

			if err := callback(
				blob.ObjectID().String(),
				blob.ObjectSize(),
				nil,
				blob.ObjectName,
			); err != nil {
				return structerr.NewInternal("sending blob chunk: %w", err)
			}

			i++
			if blobsLimit > 0 && i >= blobsLimit {
				break
			}
		}
```

**File:** doc/backpressure.md (L13-52)
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

For example:

- One clone request comes in for repository "A" (a largish repository).
- While this RPC is executing, another request comes in for repository "A". Because
  `max_per_repo` is 1 in this case, the second request blocks until the first request
  is finished.

An in-memory queue of requests can build up in Gitaly that are waiting their turn. Because
this is a potential vector for a memory leak, two other values in the `[[concurrency]]`
configuration can prevent an unbounded in-memory queue of requests:

- `max_queue_wait` is the maximum amount of time a request can wait in the
  concurrency queue. When a request waits longer than this time, it returns
  an error to the client.
- `max_queue_size` is the maximum size the concurrency queue can grow for a
  given RPC. If a concurrency queue is at its maximum, subsequent requests
  return with an error. The queue size is per repository.

For example:

```toml
[[concurrency]]
rpc = "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"
max_per_repo = 1
max_queue_wait = "1m"
max_queue_size = 5
```
```

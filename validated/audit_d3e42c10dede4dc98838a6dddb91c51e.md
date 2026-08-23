### Title
Unbounded per-blob catfile processing loop in `GetLFSPointers` enables RPC-handler resource-exhaustion DoS - ([File: internal/gitaly/service/blob/lfs_pointers.go])

### Summary
`GetLFSPointers` builds an in-memory slice sized to the client-supplied `BlobIds` list and feeds every entry into a `catfile` object/info pipeline, with no upper bound on the number of blob IDs a client may request in a single RPC call, unlike its sibling RPCs (`ListBlobs`/`ListAllBlobs`) which enforce a `blobsLimit`.

### Finding Description
`validateGetLFSPointersRequest` only rejects an empty `BlobIds` list; it never caps its length: [1](#0-0) 

`GetLFSPointers` then allocates a slice equal in length to the untrusted `BlobIds` array and drives per-item work through the object-info reader and object reader (each backed by long-lived `git cat-file --batch`/`--batch-check` processes): [2](#0-1) 

Unlike `ListBlobs`/`ListAllBlobs`, which pass a caller-facing `limit` into `processBlobs`/`sendLFSPointers` to bound the number of items processed per call, `GetLFSPointers` calls `sendLFSPointers(chunker, catfileObjectIter, 0)` — a `0` limit which the loop treats as "no cap" (`if limit > 0 && i >= limit { break }`): [3](#0-2) 

This mirrors the bug class in the referenced report: a loop bound (there, the snapshot-ID gap; here, the number of blob IDs) is entirely attacker-controlled and unenforced, and each iteration performs non-trivial work (a `cat-file` round trip plus a data copy), so the total cost of a single RPC scales linearly with an attacker-chosen input with no server-side ceiling.

### Impact Explanation
A caller of `GetLFSPointers` (reachable by any client authorized to call Gitaly's `BlobService`, e.g. GitLab Rails/Workhorse relaying a client-influenced list of blob OIDs) can submit an arbitrarily large `BlobIds` array (bounded only by the gRPC message size, which for short OID strings can still contain a very large number of entries). Processing walks the whole list through the catfile pipeline, consuming CPU, catfile-process I/O, and per-item allocations, which can degrade or exhaust resources for the serving Gitaly node/repository, denying service to other RPCs on that repository.

### Likelihood Explanation
Likelihood is moderate: the RPC is reachable without any special privilege beyond normal repository access, requires no repository state manipulation, and only requires constructing a request with many blob IDs (they need not even resolve to real LFS pointers — only `objectInfo.Type != "blob"` entries are filtered, and the info reader still processes each requested ID).

### Recommendation
Enforce a maximum on `len(req.GetBlobIds())` in `validateGetLFSPointersRequest` (returning `InvalidArgument` when exceeded), and/or thread a real `limit` (or configurable server-side cap) into `sendLFSPointers` for `GetLFSPointers` the same way `ListBlobs`/`ListAllBlobs` already do, so a single call cannot force unbounded catfile work.

### Proof of Concept
1. Construct a `GetLFSPointersRequest` with `BlobIds` populated with a very large number of short strings (e.g. hundreds of thousands of 40-character placeholder OIDs), staying under the gRPC max message size.
2. Call `BlobService.GetLFSPointers` against a Gitaly node.
3. Observe that `blobs := make([]gitpipe.RevisionResult, len(req.GetBlobIds()))` allocates proportionally, and the subsequent `gitpipe.CatfileInfo`/`gitpipe.CatfileObject` pipeline processes every entry with `sendLFSPointers(..., 0)` (no cap), driving sustained CPU/catfile load compared to the bounded `ListBlobs` path which would refuse to keep processing past its `limit`.

### Citations

**File:** internal/gitaly/service/blob/lfs_pointers.go (L145-181)
```go
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

	catfileInfoIter, err := gitpipe.CatfileInfo(ctx, objectInfoReader, gitpipe.NewRevisionIterator(ctx, blobs),
		gitpipe.WithSkipCatfileInfoResult(func(objectInfo *catfile.ObjectInfo) bool {
			return objectInfo.Type != "blob" || objectInfo.Size > lfsPointerMaxSize
		}),
	)
	if err != nil {
		return structerr.NewInternal("creating object info iterator: %w", err)
	}

	catfileObjectIter, err := gitpipe.CatfileObject(ctx, objectReader, catfileInfoIter)
	if err != nil {
		return structerr.NewInternal("creating object iterator: %w", err)
	}

	if err := sendLFSPointers(chunker, catfileObjectIter, 0); err != nil {
		return err
	}

	return nil
}
```

**File:** internal/gitaly/service/blob/lfs_pointers.go (L183-193)
```go
func validateGetLFSPointersRequest(ctx context.Context, locator storage.Locator, req *gitalypb.GetLFSPointersRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetBlobIds()) == 0 {
		return fmt.Errorf("empty BlobIds")
	}

	return nil
}
```

**File:** internal/gitaly/service/blob/lfs_pointers.go (L212-252)
```go
func sendLFSPointers(chunker *chunk.Chunker, iter gitpipe.CatfileObjectIterator, limit int) error {
	buffer := bytes.NewBuffer(make([]byte, 0, lfsPointerMaxSize))

	var i int
	for iter.Next() {
		lfsPointer := iter.Result()

		// Avoid allocating bytes for an LFS pointer until we know the current blob really
		// is an LFS pointer.
		buffer.Reset()

		// Given that we filter pipeline objects by size, the biggest object we may see here
		// is 200 bytes in size. So it's not much of a problem to read this into memory
		// completely.
		if _, err := io.Copy(buffer, lfsPointer); err != nil {
			return structerr.NewInternal("reading LFS pointer data: %w", err)
		}

		pointer, fileOid, fileSize := git.IsLFSPointer(buffer.Bytes())
		if !pointer {
			continue
		}

		objectData := make([]byte, buffer.Len())
		copy(objectData, buffer.Bytes())

		if err := chunker.Send(&gitalypb.LFSPointer{
			Data:     objectData,
			Size:     int64(len(objectData)),
			Oid:      lfsPointer.ObjectID().String(),
			FileOid:  fileOid,
			FileSize: fileSize,
		}); err != nil {
			return structerr.NewInternal("sending LFS pointer chunk: %w", err)
		}

		i++
		if limit > 0 && i >= limit {
			break
		}
	}
```

### Title
Unbounded `RevisionPaths` Array in `GetBlobsRequest` Enables Per-Request CPU/DoS Amplification - (`File: internal/gitaly/service/blob/get_blobs.go`)

### Summary
`GetBlobs` accepts a `repeated RevisionPath revision_paths` field from any caller with access to the RPC and performs a full tree/path lookup for every single entry, synchronously, in one gRPC handler invocation, with no minimum size, no per-request maximum, and no batching cost check — mirroring the reported flaw where a flat, guaranteed unit-of-work is charged per array entry regardless of whether the entry carries any real payload.

### Finding Description
`validateGetBlobsRequest` only checks that `RevisionPaths` is non-empty and that each revision string is syntactically valid; it enforces no upper bound on `len(req.GetRevisionPaths())`: [1](#0-0) 

`sendGetBlobsResponse` then iterates over every entry of that attacker-controlled slice and performs a `FindByRevisionAndPath` tree walk (plus, for real blobs, an additional `objectInfoReader.Info` call) per entry, unconditionally: [2](#0-1) [3](#0-2) 

This is structurally the same pattern as the reported bug: a single request can contain arbitrarily many "entries" (there, `claimerToBuyers`; here, `revision_paths`), each of which is unconditionally charged a fixed unit of work (there, `claimFlatFee`; here, a `catfile` tree traversal + object-info lookup), even when the entry is trivial or degenerate (e.g., empty path, non-existent revision, path pointing at the same tiny blob repeated thousands of times). There is no "minimum purchase"-equivalent check — no minimum path complexity or request-size floor — and no maximum either, so the only practical ceiling is the default gRPC max message size, which still permits tens of thousands of small `RevisionPath` entries in a single request.

### Impact Explanation
An unprivileged, authenticated caller of `BlobService.GetBlobs` (e.g., via the GitLab web/API layer that proxies to Gitaly) can submit one request containing a very large `revision_paths` array, forcing the Gitaly node to perform a correspondingly large number of synchronous tree-walk and object-info lookups on a single `catfile` session inside one RPC handler goroutine. Because processing is strictly sequential per array entry with no size/complexity gate, this ties up a `catfile` reader/worker and CPU for the duration of the request and can be repeated concurrently across many connections to exhaust `catfile` cache slots, file descriptors, and CPU on the storage node — a handler-level resource-exhaustion condition analogous to the reported "expensive/impossible to execute" claim scenario, though bounded by gRPC message size rather than unbounded on-chain state.

### Likelihood Explanation
Likelihood is moderate: the RPC is reachable by any caller with read access to a repository (an "ordinary user" fetch/browse path), requires no privileged role, and the request itself is trivial to construct (a large array of small/duplicate `RevisionPath` entries). The only mitigating factor is the implicit cap imposed by gRPC's default max receive message size, which limits — but does not eliminate — the achievable array size and thus the achievable amplification.

### Recommendation
Enforce an explicit maximum on `len(req.GetRevisionPaths())` (and equivalently for other unbounded repeated fields consumed per-entry, such as `ListBlobsRequest.revisions`) in `validateGetBlobsRequest`, rejecting requests above a sane threshold with `InvalidArgument`, mirroring the audit's "set a minimum/maximum unit of processable work per request" remediation. Consider also de-duplicating identical `(revision, path)` pairs before processing to avoid trivially cheap amplification via repeated entries.

### Proof of Concept
1. As any user authorized to call `GetBlobs` on a repository, construct a `GetBlobsRequest` with `revision_paths` containing N (e.g., 50,000) entries, each `{revision: "HEAD", path: ""}` or pointing at distinct-but-trivial paths, staying under the default gRPC message size limit.
2. Send the request; observe that `sendGetBlobsResponse` performs N sequential `FindByRevisionAndPath` (and possibly N `objectInfoReader.Info`) calls with no early rejection based on array size.
3. Issue several such requests concurrently to amplify CPU/catfile-cache pressure on the Gitaly node, demonstrating handler-level resource exhaustion without any privileged access.

### Citations

**File:** internal/gitaly/service/blob/get_blobs.go (L31-45)
```go
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

```

**File:** internal/gitaly/service/blob/get_blobs.go (L70-76)
```go
		objectInfo, err := objectInfoReader.Info(ctx, git.Revision(treeEntry.GetOid()))
		if err != nil {
			return structerr.NewInternal("read object info: %w", err)
		}

		response.Size = objectInfo.Size

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

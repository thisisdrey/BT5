Based on my investigation, I found a concrete analog in `internal/gitaly/service/remote/update_remote_mirror.go`.

### Title
Unbounded growth of `branchMatchers` slice in `UpdateRemoteMirror` client stream enables O(n·m) reference-matching DoS - (File: internal/gitaly/service/remote/update_remote_mirror.go)

### Summary
`UpdateRemoteMirror` is a client-streaming MUTATOR RPC. The handler accumulates the `OnlyBranchesMatching` repeated field from every message in the incoming stream into a single, ever-growing `branchMatchers` slice with no upper bound, then compiles it into a `referenceMatcher` that is subsequently evaluated against every remote and local reference of the repository.

### Finding Description
The handler reads the first request, then loops over all subsequent stream messages, appending each message's `OnlyBranchesMatching` values into `branchMatchers` without ever capping the slice size or the number of messages processed: [1](#0-0) 

```go
func (s *server) updateRemoteMirror(stream gitalypb.RemoteService_UpdateRemoteMirrorServer, firstRequest *gitalypb.UpdateRemoteMirrorRequest) error {
	ctx := stream.Context()

	branchMatchers := firstRequest.GetOnlyBranchesMatching()
	for {
		req, err := stream.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return fmt.Errorf("receive: %w", err)
		}
		branchMatchers = append(branchMatchers, req.GetOnlyBranchesMatching()...)
	}
```

This is structurally the same defect class as the auction contract's `auctionInfoData[_tokenid]` array: a client-controlled collection that only grows (`push`/`append`) and is never bounded, then later consumed in a loop whose cost scales with its length. Here, `branchMatchers` is compiled into a `referenceMatcher` and then evaluated for every remote reference and every local reference fetched from the repository: [2](#0-1) 

Because `referenceMatcher` construction/evaluation cost is proportional to the number of matcher patterns, and the number of references being matched can itself be large (all `refs/heads/*` and `refs/tags/*`), the total cost of the RPC handler is O(patterns × refs). An ordinary, authenticated client that can call `UpdateRemoteMirror` (e.g., via a mirroring push flow) fully controls the number of `OnlyBranchesMatching` entries it sends across the stream — there is no limit on the number of gRPC messages nor on the number of strings per message.

### Impact Explanation
A caller of `UpdateRemoteMirror` can send an arbitrarily large number of `OnlyBranchesMatching` glob patterns spread across many stream messages, causing the server to allocate and later iterate/match against a huge matcher slice for every reference in the repository. This can consume excessive CPU and memory in the Gitaly process handling the RPC, degrading or denying service for that RPC (and, depending on resource contention, other RPCs on the same node), directly matching the report's "unbounded loop over never-shrinking, client-controlled array" DoS pattern.

### Likelihood Explanation
`UpdateRemoteMirror` is invoked by ordinary GitLab remote-mirroring functionality and does not require any special/administrative privilege beyond the ability to trigger a mirror update on a repository the caller controls (a mutator RPC reachable through normal usage, not a leaked-token/MITM/malicious-peer scenario). The request/stream construction is entirely client-controlled, so exploitation only requires crafting a stream with many `OnlyBranchesMatching` entries — no race condition or unusual timing is needed, making this readily reachable.

### Recommendation
Bound the total number of `OnlyBranchesMatching` entries accepted across the entire stream (and/or the total accumulated byte size), rejecting the request with `InvalidArgument`/`ResourceExhausted` once a configurable maximum is exceeded, analogous to other Gitaly limits such as `objectPoolMembersBatchSize`/batched processing seen elsewhere in the codebase [3](#0-2) . Additionally, consider capping how many stream messages will be consumed before erroring out, so a client cannot force unbounded memory growth simply by keeping the stream open.

### Proof of Concept
1. An authenticated client with mirroring permissions on a repository opens a `RemoteService.UpdateRemoteMirror` client-streaming call.
2. The client sends the required first request, then sends many additional `UpdateRemoteMirrorRequest` messages, each containing a large `OnlyBranchesMatching` array (e.g., thousands of unique glob patterns per message, repeated over many messages).
3. `updateRemoteMirror` accumulates all of these into `branchMatchers` with no limit [4](#0-3) .
4. `newReferenceMatcher(branchMatchers)` compiles the full set, and the resulting matcher is then evaluated against every local/remote reference of the repository, causing CPU/memory usage proportional to `len(branchMatchers) * len(refs)`, degrading the Gitaly node's ability to serve this and potentially other requests.

**Note on completeness:** I was unable to open the full `update_remote_mirror.go` file (tool call failed on the final iteration) to confirm the exact downstream loop where `referenceMatcher` is applied to each reference and to check whether any existing size/message-count limit exists elsewhere in the request validation path. This should be verified by reading the complete file and `newReferenceMatcher`'s implementation before treating this as confirmed exploitable, though the accumulation loop itself is confirmed unbounded from the visible code.

### Citations

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L45-60)
```go
func (s *server) updateRemoteMirror(stream gitalypb.RemoteService_UpdateRemoteMirrorServer, firstRequest *gitalypb.UpdateRemoteMirrorRequest) error {
	ctx := stream.Context()

	branchMatchers := firstRequest.GetOnlyBranchesMatching()
	for {
		req, err := stream.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return fmt.Errorf("receive: %w", err)
		}

		branchMatchers = append(branchMatchers, req.GetOnlyBranchesMatching()...)
	}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L106-118)
```go
	remoteRefsSlice, err := repo.GetRemoteReferences(ctx, remoteName,
		localrepo.WithPatterns("refs/heads/*", "refs/tags/*"),
		localrepo.WithConfig(remoteConfig...),
		localrepo.WithSSHCommand(sshCommand),
	)
	if err != nil {
		return fmt.Errorf("get remote references: %w", err)
	}

	localRefs, err := repo.GetReferences(ctx, "refs/heads/", "refs/tags/")
	if err != nil {
		return fmt.Errorf("get local references: %w", err)
	}
```

**File:** internal/gitaly/service/internalgitaly/list_pool_upstreams.go (L12-14)
```go
// objectPoolMembersBatchSize is the maximum number of pool disk paths to send in a single
// request to the Rails ObjectPoolMembers API. The Rails endpoint enforces a limit of 500.
const objectPoolMembersBatchSize = 500
```

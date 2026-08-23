I found a strong analog in gitaly's `computeCacheKey` function for the pack-objects cache. This mirrors the Swapper bug's root cause exactly: a security/behavior-relevant decision is keyed on a **rewritten/normalized identifier that differs from the actual operative identifier**, causing configuration/limits meant for one identity to silently not apply to (or incorrectly apply to) another.

### Title
Pack-objects cache key omits per-request identity fields normalized away by transaction rewriting, causing cross-request cache key collisions across distinct security contexts - (File: internal/gitaly/service/hook/pack_objects.go)

### Summary
The Swapper bug's root cause is that a discount/override table is keyed on a *converted* representation of the pair (post `_convert()`/`_sort()`) rather than the actual (base, quote) tokens the owner configured, so overrides silently fail to apply to the intended real-world identity. The closest reachable analog in Gitaly is `computeCacheKey` in `internal/gitaly/service/hook/pack_objects.go`, which deliberately restores the repository's pre-transaction identity (`tx.OriginalRepository`) before hashing the cache key, but the fields chosen for the key (`Repository`, `Args`, `GitProtocol`, and hashed stdin) do not include the requester's identity (`GlId`/`RemoteIp`), even though the RPC handler (`runPackObjectsLimited`) uses `RemoteIp` to decide whether the request is subject to `PackObjectsLimiter` at all.

### Finding Description
In `computeCacheKey` (`internal/gitaly/service/hook/pack_objects.go:117-145`), the cache key is derived from `Repository`, `Args`, `GitProtocol`, and the SHA-256 of stdin (the negotiated wants/haves): [1](#0-0) 

This mirrors the Swapper's `_convert`/`_sort` step: the repository field is *rewritten back* (converted) from the transaction snapshot identity to `tx.OriginalRepository(...)` specifically "to ensure identical requests get the same key" — exactly analogous to how the Swapper oracle converts ETH↔WETH so that logically-identical pairs hash to the same lookup key. However, `packObjectsHook` (`internal/gitaly/service/hook/pack_objects.go:47-112`) branches on `req.GetRemoteIp()` to decide whether `runPackObjectsLimited` (which enforces `PackObjectsLimiter`, a per-IP/repo/user concurrency and rate limit) or the unlimited `runPackObjects` path is used — but `RemoteIp` and `GlId` are excluded from the cache key computation entirely: [2](#0-1) 

Because the cache is checked and populated with `s.packObjectsCache.Fetch(ctx, cacheKey, ...)` before the per-request limiter decision is made, and the key generation intentionally normalizes away the identity fields that drive that decision, a request whose true identity would be subject to `PackObjectsLimiter` (e.g. non-loopback `RemoteIp`) can transparently reuse a cache entry that was created by — or is subsequently reused by — a request from a different `RemoteIp`/`GlId` that bypassed the limiter path (loopback IP), or vice versa. This is structurally identical to the Swapper flaw: the "override" (concurrency/rate limiting decision) is keyed on the raw request fields, while the "lookup"/dedup mechanism (cache key) is keyed on a converted/normalized form that discards precisely the fields the override needs to distinguish.

### Impact Explanation
An attacker controlling `RemoteIp`/`GlId` values on the `PackObjectsHookWithSidechannelRequest` (reachable via the `gitaly-hooks` `PackObjectsHook` path invoked during every fetch/clone) could structure concurrent requests so that the expensive `git-pack-objects` computation performed under one identity's limiter budget is served via cache hit to requests whose identity would otherwise be throttled by `PackObjectsLimiter`, undermining the per-IP/per-repo/per-user backpressure mechanism documented in `doc/design_pack_objects_cache.md` and `doc/backpressure.md`. This weakens Gitaly's primary DoS-protection mechanism for the most expensive Git operation (pack generation) without requiring any privileged access — it only requires the ability to issue fetch/clone requests with attacker-influenced `RemoteIp`/`GlId` metadata.

### Likelihood Explanation
`RemoteIp` and `GlId` are populated from request metadata passed through `gitaly-hooks`/`SetupSidechannel` on every fetch, and the existing test suite (`pack_objects_test.go`) explicitly documents that different `RemoteIp` and `GlId` values are *treated identically* for cache purposes today ("requests from different remote IPs" / "requests from different user IDs" test cases both expect `shouldUseCacheOf: []int{-1, 0, 0, 0, 0}"), confirming the cache key omission is a real, exercised code path rather than a theoretical corner case. [3](#0-2) 

### Recommendation
Include the fields that drive the `PackObjectsLimiter` gating decision (at minimum a normalized/bucketed form of `RemoteIp` and `GlId`) in `computeCacheKey`, or move the limiter decision to occur strictly before any cache lookup/population so that the caching layer cannot be used to bypass the per-identity concurrency/rate limit — mirroring the general fix pattern from the Swapper report of moving the override/policy decision to operate on the real, un-normalized identity rather than on a form that has already discarded distinguishing information.

### Citations

**File:** internal/gitaly/service/hook/pack_objects.go (L65-97)
```go
	servedBytes, created, err := s.packObjectsCache.Fetch(ctx, cacheKey, output, func(w io.Writer) error {
		ipAddr := net.ParseIP(req.GetRemoteIp())
		if ipAddr == nil {
			// Best effort, maybe the remote IP includes source port
			if ip, _, err := net.SplitHostPort(req.GetRemoteIp()); err == nil {
				ipAddr = net.ParseIP(ip)
			}
		}
		// Ignore loop-back IPs
		if ipAddr != nil && !ipAddr.IsLoopback() {
			return s.runPackObjectsLimited(
				ctx,
				w,
				ipAddr.String(),
				req,
				args,
				stdin,
				cacheKey,
			)
		}

		return s.runPackObjects(ctx, w, req, args, stdin, cacheKey)
	})
	if err != nil {
		return err
	}

	if created {
		closeStdin = false
		packObjectsCacheLookups.WithLabelValues("miss").Inc()
	} else {
		packObjectsCacheLookups.WithLabelValues("hit").Inc()
	}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L117-145)
```go
func (s *server) computeCacheKey(ctx context.Context, req *gitalypb.PackObjectsHookWithSidechannelRequest, stdinReader io.Reader) (string, io.ReadCloser, error) {
	cacheHash := sha256.New()

	repository := req.GetRepository()
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		// The cache uses the requests as the keys. As the request's repository in the RPC handler has been rewritten
		// to point to the transaction's repository, the handler sees each request as different even if they point to
		// the same repository. Restore the original request to ensure identical requests get the same key.
		repository = tx.OriginalRepository(req.GetRepository())
	}

	cacheKeyPrefix, err := protojson.Marshal(&gitalypb.PackObjectsHookWithSidechannelRequest{
		Repository:  repository,
		Args:        req.GetArgs(),
		GitProtocol: req.GetGitProtocol(),
	})
	if err != nil {
		return "", nil, err
	}
	if _, err := cacheHash.Write(cacheKeyPrefix); err != nil {
		return "", nil, err
	}
	stdin, err := bufferStdin(stdinReader, cacheHash)
	if err != nil {
		return "", nil, err
	}
	cacheKey := hex.EncodeToString(cacheHash.Sum(nil))
	return cacheKey, stdin, nil
}
```

**File:** internal/gitaly/service/hook/pack_objects_test.go (L317-345)
```go
			name: "requests from different remote IPs",
			makeRequests: func(repository *gitalypb.Repository) []*gitalypb.PackObjectsHookWithSidechannelRequest {
				args := []string{"pack-objects", "--revs", "--thin", "--stdout", "--progress", "--delta-base-offset"}
				return []*gitalypb.PackObjectsHookWithSidechannelRequest{
					{Repository: repository, Args: args, RemoteIp: "1.2.3.4"},
					{Repository: repository, Args: args, RemoteIp: "1.2.3.5"},
					{Repository: repository, Args: args, RemoteIp: "1.2.3.4"},
					{Repository: repository, Args: args, RemoteIp: "1.2.3.4"},
					{Repository: repository, Args: args, RemoteIp: "1.2.3.5"},
				}
			},
			// All from cached
			shouldUseCacheOf: []int{-1, 0, 0, 0, 0},
		},
		{
			name: "requests from different user IDs",
			makeRequests: func(repository *gitalypb.Repository) []*gitalypb.PackObjectsHookWithSidechannelRequest {
				args := []string{"pack-objects", "--revs", "--thin", "--stdout", "--progress", "--delta-base-offset"}
				return []*gitalypb.PackObjectsHookWithSidechannelRequest{
					{Repository: repository, Args: args, GlId: "1"},
					{Repository: repository, Args: args, GlId: "1"},
					{Repository: repository, Args: args, GlId: "1"},
					{Repository: repository, Args: args, GlId: "2"},
					{Repository: repository, Args: args, GlId: "3"},
				}
			},
			// All from cached
			shouldUseCacheOf: []int{-1, 0, 0, 0, 0},
		},
```

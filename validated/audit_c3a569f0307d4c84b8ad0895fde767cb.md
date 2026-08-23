I found the concrete analog: the `x-gitaly-cost` computation in `costhandler.go`.

### Title
Client-Controllable Cost-Score Truncation Lets Attackers Split Large Requests to Evade Rate-Limit Accounting - (File: internal/grpc/middleware/costhandler/costhandler.go)

### Summary
`computeCost` derives the `x-gitaly-cost` gRPC trailer (forwarded by Rails/Workhorse as the `X-Score` HTTP header used for upstream, e.g. Cloudflare, rate limiting) from a static per-RPC weight plus a dynamic component computed with integer division/ceiling over total payload bytes. Because the dynamic cost is computed and rounded independently per RPC call, an attacker can split a single large data-transfer operation into many smaller RPC calls, each just under a `byteCostDivisor` boundary, and have the sum of reported costs be far smaller than the cost that a single equivalent transfer would report — exactly the "many small operations vs one large operation" integer-truncation pattern described in the reference report.

### Finding Description
`dynamicCostFromContext` computes:
```go
totalBytes := stats.InPayloadBytes + stats.OutPayloadBytes
return int(math.Ceil(float64(totalBytes) / float64(byteCostDivisor)))
``` [1](#0-0) 

with `byteCostDivisor` fixed at 1 MiB: [2](#0-1) 

`computeCost` combines this with a static per-method weight and is invoked once per unary/streaming RPC via `UnaryInterceptor`/`StreamInterceptor`, which set the `x-gitaly-cost` trailer on every RPC response: [3](#0-2) 

Because `Ceil(bytes/1MiB)` rounds *up* per call but the static weight is fixed per call, a caller who splits one N-MiB operation into `k` separate RPC calls each carrying just under 1 MiB (e.g. by chunking blob reads/writes, using narrower request parameters, or issuing many small accessor RPCs instead of one large streaming RPC) pays the static cost `k` times but the aggregate dynamic cost stays close to `k` (one unit each) instead of `N` units that a single request would have accrued, while at the same time the *actual* static-cost model for cheap accessor RPCs (`defaultAccessorCost = 1`) is far lower than the override used for expensive streaming operations (e.g., `PostUploadPackWithSidechannel` = 10). This lets a client architect equivalent data movement through many cheap-classified RPCs to keep its reported `x-gitaly-cost`/`X-Score` low relative to actual resource consumption, exactly mirroring the reference bug where `(rate * shortPeriod) * repetitions` computed via truncating integer math undershoots the value of `rate * fullPeriod` computed once.

The list of static overrides is a fixed map keyed by RPC name, so any RPC that isn't in `staticCostOverrides` — including large-object enumeration and diff endpoints not listed, or future RPCs — falls back to the operation type default (accessor=1, mutator=5, maintenance=3, unknown=1), independent of how many bytes it actually transfers beyond the ceiling division: [4](#0-3) 

### Impact Explanation
This is a rate-limiting/accounting-evasion vulnerability, not a memory-safety or auth bypass one: the `x-gitaly-cost` value is the sole signal Rails/Workhorse forward upstream (e.g. to Cloudflare's complexity-based rate limiter) for throttling clients, per the documented architecture: [5](#0-4) 
An unprivileged, ordinary client that structures its traffic as many small RPCs instead of few large ones causes the aggregate reported cost to systematically undercount true resource consumption, weakening the upstream rate limiter's ability to throttle it — a resource-exhaustion/DoS-adjacent condition on the shared Gitaly node, since the downstream circuit breaker/rate limiter is the only defense against exactly this kind of load.

### Likelihood Explanation
High: any authenticated Gitaly/GitLab client can freely choose how to batch or split its requests (e.g., fetching blobs in a loop instead of one bulk request), and no additional privilege or crafted internal field is required — this is purely a client request-shaping decision, reachable through ordinary API/Git usage.

### Recommendation
Compute the dynamic cost using an accumulation model that does not independently round each call (e.g., accumulate fractional cost with a stateful, per-client/per-session running total, or use a non-ceiling proportional cost such as `bytes/byteCostDivisor` with a minimum floor added once at the boundary), and consider tracking effective aggregate bytes per identity/window rather than trusting a purely per-RPC computed and independently-rounded score.

### Proof of Concept
1. As an ordinary authenticated client, transfer `N` MiB of data via a single RPC that is not in `staticCostOverrides` (falls back to `defaultAccessorCost = 1`); the trailer cost is `1 + ceil(N)`.
2. Split the same `N` MiB of data across `k = N` separate RPC calls of ~1 MiB (or just under 1 MiB) each; each call reports cost `1 + 1 = 2` (static + `ceil(~1MiB/1MiB)`), for a per-call cost identical to a single 1 MiB call. Compare aggregate reported cost across the `k` split calls (`~2k`) against what a single call moving the same bytes would need to report proportionally if costs scaled linearly — because `byteCostDivisor` rounds each call up to at least 1 unit regardless of how small the tail chunk is, and static cost dominates for small transfers, the attacker can tune chunk size to minimize `dynamic` contribution relative to bytes actually moved, in direct analogy to the reference report's "365 one-day loans vs one 365-day loan" truncation exploit.

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L25-58)
```go
const (
	defaultAccessorCost    = 1
	defaultMutatorCost     = 5
	defaultMaintenanceCost = 3
	defaultUnknownCost     = 1
)

// byteCostDivisor controls how payload bytes contribute to the cost score.
// Every byteCostDivisor bytes adds 1 to the cost.
const byteCostDivisor = 1 << 20 // 1 MiB

// staticCostOverrides allows per-RPC cost overrides for RPCs that are known to
// be especially cheap or expensive relative to their operation type default.
var staticCostOverrides = map[string]int{
	// Streaming RPCs that transfer large amounts of data.
	"/gitaly.SmartHTTPService/PostUploadPackWithSidechannel": 10,
	"/gitaly.SmartHTTPService/PostReceivePack":               10,
	"/gitaly.SSHService/SSHUploadPack":                       10,
	"/gitaly.SSHService/SSHUploadPackWithSidechannel":        10,
	"/gitaly.SSHService/SSHReceivePack":                      10,

	// Large-object enumeration or diff operations.
	"/gitaly.DiffService/CommitDiff":    8,
	"/gitaly.DiffService/RawDiff":       8,
	"/gitaly.DiffService/RawPatch":      8,
	"/gitaly.BlobService/GetBlobs":      6,
	"/gitaly.BlobService/ListBlobs":     6,
	"/gitaly.CommitService/ListCommits": 6,

	// Lightweight RPCs.
	"/gitaly.RefService/FindDefaultBranchName":   1,
	"/gitaly.RepositoryService/RepositoryExists": 1,
	"/gitaly.ServerService/ServerInfo":           0,
}
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L60-91)
```go
// UnaryInterceptor is a gRPC unary server interceptor that sets the
// x-gitaly-cost trailer after the handler completes.
func UnaryInterceptor(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
	resp, err := handler(ctx, req)

	_ = grpc.SetTrailer(ctx, metadata.Pairs(
		CostHeader, fmt.Sprintf("%d", computeCost(ctx, info.FullMethod)),
	))

	return resp, err
}

// StreamInterceptor is a gRPC stream server interceptor that sets the
// x-gitaly-cost trailer after the handler completes.
func StreamInterceptor(srv interface{}, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	err := handler(srv, stream)

	stream.SetTrailer(metadata.Pairs(
		CostHeader, fmt.Sprintf("%d", computeCost(stream.Context(), info.FullMethod)),
	))

	return err
}

// computeCost returns the cost score for a completed RPC. It combines a static
// base cost for the RPC type with a dynamic component from actual bytes
// transferred, read from the RPCEntry in context.
func computeCost(ctx context.Context, fullMethod string) int {
	static := staticCostForMethod(fullMethod)
	dynamic := dynamicCostFromContext(ctx)
	return static + dynamic
}
```

**File:** internal/grpc/middleware/costhandler/costhandler.go (L118-128)
```go
// dynamicCostFromContext computes the dynamic cost contribution from payload
// bytes tracked by the grpcstats.PayloadBytes stats handler.
func dynamicCostFromContext(ctx context.Context) int {
	stats := grpcstats.PayloadBytesStatsFromContext(ctx)
	if stats == nil {
		return 0
	}
	totalBytes := stats.InPayloadBytes + stats.OutPayloadBytes

	return int(math.Ceil(float64(totalBytes) / float64(byteCostDivisor)))
}
```

**File:** doc/load-management-architecture.md (L269-286)
```markdown
### RPC Cost Score (`x-gitaly-cost`)

Gitaly returns a cost score for each RPC as a gRPC response trailer.
Gitaly has the most context about the actual cost of each RPC, making it the right
place to own this value. Rails and Workhorse translate the `x-gitaly-cost`
trailer into an `X-Score` HTTP response header, making the cost signal
available to any upstream rate limiter.

> On GitLab.com, the `X-Score` header feeds Cloudflare's
> [complexity-based rate limiting](https://developers.cloudflare.com/waf/rate-limiting-rules/request-rate/#complexity-based-rate-limiting).
> Self-managed deployments can use the same header with any upstream
> rate limiter or ignore it.

Static scores are derived from historical data per RPC type. A static score hides
variance (e.g. `PostUploadPackWithSidechannel` is the same cost whether it serves
1 MB or 10 GB), but dynamic cost (bytes transferred, object count) is only known
after the RPC completes. The two approaches can be combined: use the static score
as a base, then reconcile actual cost in a follow-up after Gitaly responds.
```

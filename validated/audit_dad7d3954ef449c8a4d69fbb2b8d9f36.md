### Title
`x-gitaly-cost` Rate-Limit Signal Excludes Sidechannel Bulk Data, Allowing Large Git Transfers to Evade Complexity-Based Rate Limiting - ([File: internal/grpc/middleware/costhandler/costhandler.go])

### Summary
The `x-gitaly-cost` trailer, which Rails/Workhorse forward to Cloudflare as the `X-Score` header for complexity-based rate limiting, is computed from a static per-RPC weight plus a dynamic component derived solely from gRPC `InPayload`/`OutPayload` byte counts [1](#0-0) . For sidechannel RPCs such as `PostUploadPackWithSidechannel`, the actual bulk packfile data is transferred entirely outside the gRPC message stream, over a separate Yamux stream [2](#0-1) , so this bulk data is never captured by `grpcstats.PayloadBytes.HandleRPC`, which only counts `stats.InPayload`/`stats.OutPayload` gRPC events [3](#0-2) .

### Finding Description
`computeCost` sums a static cost (10 for `PostUploadPackWithSidechannel`, per `staticCostOverrides`) with a dynamic cost of `ceil(totalBytes / 1MiB)`, where `totalBytes` comes exclusively from `grpcstats.PayloadBytesStatsFromContext` [4](#0-3) . However, `PostUploadPackWithSidechannel`'s actual packfile payload — potentially gigabytes for a full clone — is streamed via `sidechannel.OpenSidechannel`, a raw Yamux-multiplexed connection that bypasses gRPC Protobuf messages entirely and is used specifically because "per-message overhead...acted as a limiting factor on how much Git fetch traffic a Gitaly server could serve" [5](#0-4) [6](#0-5) . Because the sidechannel bytes never pass through `stats.InPayload`/`stats.OutPayload`, `dynamicCostFromContext` sees only the small gRPC control message (repository reference, git-protocol negotiation) and stays near zero regardless of how large the actual clone/fetch is. This is structurally the same bug pattern as the referenced report: a fee/cost value is derived from a partial, attacker-influenceable proxy of the real transferred amount (the gRPC-visible bytes) rather than the true amount (the sidechannel-transferred packfile bytes), letting the true "amount" bypass the calculation entirely.

The design doc itself acknowledges the static score alone "hides variance (e.g. `PostUploadPackWithSidechannel` is the same cost whether it serves 1 MB or 10 GB)" and states dynamic cost is meant to compensate, but the dynamic component's data source (`grpcstats.PayloadBytes`) structurally cannot observe sidechannel traffic [7](#0-6) .

### Impact Explanation
Since `x-gitaly-cost` is the sole per-RPC input to Cloudflare's complexity-based rate limiting and to Rails' `X-Score` header [8](#0-7) , an attacker performing large repeated clones/fetches over `PostUploadPackWithSidechannel` (or `SSHUploadPackWithSidechannel`) is always scored at a near-fixed cost (static 10 + negligible dynamic), regardless of whether they transfer kilobytes or gigabytes of packfile data. This lets a client systematically evade the upstream complexity-based rate limiter/circuit breaker that GitLab.com relies on to throttle expensive Git traffic, enabling resource-exhaustion abuse (repeated large clones) that the cost-scoring mechanism was specifically designed to detect and throttle.

### Likelihood Explanation
This is reachable by any ordinary client performing a `git fetch`/clone via HTTP or SSH against Gitaly — the exact intended, unprivileged code path for `PostUploadPackWithSidechannel`/`SSHUploadPackWithSidechannel` — requiring no special access, malicious peer behavior, or leaked credentials. The condition triggers deterministically any time sidechannel is used, which is the standard transport for Git HTTP/SSH fetch traffic per the design doc.

### Recommendation
Track bytes copied over the sidechannel connection (e.g., wrap `conn` in `runUploadPack`/`OpenSidechannel` with a byte-counting reader/writer) and feed that count into the same context value read by `dynamicCostFromContext`, so the `x-gitaly-cost` dynamic component reflects total data moved (gRPC + sidechannel) rather than only gRPC-visible payload bytes. Alternatively, increase the static cost for sidechannel RPCs to scale with `PackfileNegotiationStatistics` fields already returned in the response (e.g., pack size), which is per-RPC and available before the trailer is set.

### Proof of Concept
1. Repeatedly issue `git fetch`/clone requests against a Gitaly node using `PostUploadPackWithSidechannel`, requesting large packfiles (e.g., full clone of a multi-GB repository).
2. Observe the `x-gitaly-cost` gRPC trailer returned for each call: per `TestComputeCost`, an RPC with no measurable gRPC-level bytes yields cost `10` regardless of packfile size, because the bulk pack bytes flow over the sidechannel and are invisible to `grpcstats.PayloadBytesStatsFromContext` [9](#0-8) .
3. Compare to a same-sized transfer forced through a non-sidechannel RPC (e.g., `GetBlobs`) with equivalent byte volume — its cost scales proportionally with actual bytes via `dynamicCostFromContext`, per `TestCostTrailerIntegration`'s "large payload adds proportional dynamic cost" case [10](#0-9) , demonstrating the disparity: sidechannel-transferred volume is effectively free from a rate-limiting perspective while equivalent gRPC-transferred volume is properly costed.

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L84-91)
```go
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

**File:** doc/sidechannel.md (L1-17)
```markdown
# Gitaly sidechannels

Since GitLab 14.4, Gitaly supports a custom protocol for RPCs that
transfer a high volume of byte stream data. Currently, this only
applies to `PostUploadPackWithSidechannel`, which is used for Git HTTP
traffic, as well as `SSHUploadPackWithSidechannel` for SSH traffic, as well as
`PackObjectsHookWithSidechannel`.

Prior to sidechannel, the only way for Gitaly to serve a byte stream
was to encapsulate the bytes in gRPC Protobuf messages. Because of the
per-message overhead, this acted as a limiting factor on how much Git
fetch traffic a Gitaly server could serve up.

The sidechannel protocol works around this by:

1. Allowing the Gitaly server to establish a sidechannel to the Gitaly client during an RPC call.
1. Performing the bulk data transfer on the sidechannel.
```

**File:** internal/grpc/grpcstats/stats.go (L27-36)
```go
func (s *PayloadBytes) HandleRPC(ctx context.Context, rs stats.RPCStats) {
	switch st := rs.(type) {
	case *stats.InPayload:
		bytesStats := ctx.Value(payloadBytesStatsKey{}).(*PayloadBytesStats)
		bytesStats.InPayloadBytes += int64(st.Length)
	case *stats.OutPayload:
		bytesStats := ctx.Value(payloadBytesStatsKey{}).(*PayloadBytesStats)
		bytesStats.OutPayloadBytes += int64(st.Length)
	}
}
```

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L21-44)
```go
func (s *server) PostUploadPackWithSidechannel(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest) (*gitalypb.PostUploadPackWithSidechannelResponse, error) {
	repoPath, gitConfig, err := s.validateUploadPackRequest(ctx, req)
	if err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	var sidechannelRetryableError sidechannel.RetryableError
	conn, err := sidechannel.OpenSidechannel(ctx)
	if err != nil {
		if errors.As(err, &sidechannelRetryableError) {
			// Clients of PostUploadPackWithSidechannel are configured to retry the RPC upon receiving
			// Unavailable, so it should be OK to return it in this case.
			//nolint:gitaly-linters
			return nil, structerr.NewUnavailable("open sidechannel: %w", err)
		}
		return nil, structerr.NewInternal("open sidechannel: %w", err)
	}
	defer conn.Close()

	stats, err := s.runUploadPack(ctx, req, repoPath, gitConfig, conn, conn)
	if err != nil {
		return nil, structerr.NewInternal("running upload-pack: %w", err)
	}

```

**File:** doc/load-management-architecture.md (L269-280)
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
```

**File:** doc/load-management-architecture.md (L282-286)
```markdown
Static scores are derived from historical data per RPC type. A static score hides
variance (e.g. `PostUploadPackWithSidechannel` is the same cost whether it serves
1 MB or 10 GB), but dynamic cost (bytes transferred, object count) is only known
after the RPC completes. The two approaches can be combined: use the static score
as a base, then reconcile actual cost in a follow-up after Gitaly responds.
```

**File:** internal/grpc/middleware/costhandler/costhandler_test.go (L58-71)
```go
func TestComputeCost(t *testing.T) {
	t.Run("no stats in context", func(t *testing.T) {
		ctx := context.Background()
		cost := computeCost(ctx, "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel")
		require.Equal(t, 10, cost)
	})

	t.Run("static only when no bytes", func(t *testing.T) {
		handler := &grpcstats.PayloadBytes{}
		ctx := handler.TagRPC(context.Background(), nil)

		cost := computeCost(ctx, "/gitaly.SmartHTTPService/PostUploadPackWithSidechannel")
		require.Equal(t, 10, cost)
	})
```

**File:** internal/grpc/middleware/costhandler/costhandler_test.go (L140-152)
```go
	t.Run("large payload adds proportional dynamic cost", func(t *testing.T) {
		var trailer metadata.MD
		largeBody := make([]byte, 2*1024*1024)

		_, err := testClient.UnaryCall(ctx, &grpc_testing.SimpleRequest{
			Payload: &grpc_testing.Payload{Body: largeBody},
		}, grpc.Trailer(&trailer))
		require.NoError(t, err)

		costValues := trailer.Get(CostHeader)
		require.Len(t, costValues, 1)
		require.Equal(t, "4", costValues[0])
	})
```

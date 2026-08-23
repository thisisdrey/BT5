### Title
Cost-trailer under-accounts bytes served via sidechannel RPCs, allowing rate-limit bypass for the heaviest Git operations - ([File: internal/grpc/middleware/costhandler/costhandler.go])

### Summary
The `x-gitaly-cost` gRPC trailer, which Rails/Workhorse translate into the `X-Score` header used by upstream (Cloudflare) rate limiting, is computed as `static + dynamic`, where the dynamic component is derived exclusively from `grpcstats.PayloadBytes` (standard gRPC message framing bytes). RPCs that stream the actual payload out-of-band via a sidechannel — `PostUploadPackWithSidechannel` and `SSHUploadPackWithSidechannel` — never register their transferred bytes with `grpcstats.PayloadBytes`, so `dynamicCostFromContext` always returns `0` for them regardless of how many GB were actually served, mirroring the AToken bug pattern where the accounted amount ("burn"/charge) is decoupled from the actual value transferred.

### Finding Description
`computeCost` in `internal/grpc/middleware/costhandler/costhandler.go` (lines 87-91) sums a static per-RPC weight (`staticCostForMethod`) with a dynamic weight from `dynamicCostFromContext`, which reads `grpcstats.PayloadBytesStatsFromContext(ctx)` and does `ceil((InPayloadBytes+OutPayloadBytes)/1MiB)` (lines 120-128).

`grpcstats.PayloadBytes` (`internal/grpc/grpcstats/stats.go`) is a `stats.Handler` that only observes `stats.InPayload`/`stats.OutPayload` events — these fire for bytes sent as gRPC message frames on the RPC stream itself. However, `SSHUploadPackWithSidechannel` (`internal/gitaly/service/ssh/upload_pack.go` lines 131-158) and `PostUploadPackWithSidechannel` open a raw sidechannel connection (`sidechannel.OpenSidechannel`) and write the actual pack data directly to that socket via a `helper.CountingWriter`, entirely bypassing the gRPC message-framing path that `grpcstats.PayloadBytes` observes. The RPC's own gRPC request/response messages carry only small metadata; the multi-gigabyte pack data itself is invisible to `grpcstats`.

As a result, for exactly the RPCs the codebase itself calls out as "streaming RPCs that transfer large amounts of data" (`staticCostOverrides`, lines 40-44, all fixed at `10`), the dynamic byte-based cost component is always `0`, no matter whether the clone/fetch served 1 MB or 100 GB. This is acknowledged in `doc/load-management-architecture.md` ("A static score hides variance... `PostUploadPackWithSidechannel` is the same cost whether it serves 1 MB or 10 GB") as a known limitation of the static score, but the code goes further: it also silently fails to reconcile with the dynamic component that was supposedly designed to compensate for exactly this variance, because the sidechannel bytes are structurally excluded from the `PayloadBytes` stats handler that feeds the dynamic term.

### Impact Explanation
`x-gitaly-cost` is the sole per-RPC signal Gitaly exports for upstream complexity-based rate limiting (Cloudflare/Rails `X-Score`). Since the dynamic component can never reflect the true size of sidechannel-served clones/fetches, an ordinary authenticated user can repeatedly clone/fetch large repositories (or force large packfile generation) while the reported cost stays pinned at the static baseline (`10`), identical to a clone that served a few KB. This under-prices the most resource-intensive RPCs relative to their actual server-side cost, undermining the intended complexity-based throttling and enabling sustained resource exhaustion (CPU for pack generation, disk I/O, bandwidth) while appearing "cheap" to the external rate limiter — a DoS-enabling under-billing/rate-limit-bypass analogous to the AToken bug where the accounted burn does not match the actual value moved.

### Likelihood Explanation
High for an ordinary user: fetch/clone (`git clone`, `git fetch`) via SSH or HTTP naturally routes through `SSHUploadPackWithSidechannel`/`PostUploadPackWithSidechannel`, which are exactly the RPCs affected. No privileged access, malicious peer, or special crafting is required — simply performing repeated large clones/fetches triggers the under-accounting deterministically every time, since it is a structural property of how sidechannel bytes bypass the gRPC stats path, not a race or edge case.

### Recommendation
Feed the sidechannel byte counts (already captured via `helper.CountingWriter`/`servedBytes` in `internal/gitaly/service/ssh/upload_pack.go` and `internal/gitaly/service/smarthttp/upload_pack.go`) into the same accounting structure `dynamicCostFromContext` reads (e.g., extend `grpcstats.PayloadBytesStats` or a dedicated context value that both the sidechannel writer and `costhandler` can access), so that the `x-gitaly-cost` trailer for sidechannel RPCs reflects actual bytes served rather than only in-band gRPC framing bytes. Recompute/set the trailer after the sidechannel transfer completes, not just after the wrapping RPC's own small messages.

### Proof of Concept
1. As an ordinary authenticated user with fetch access to a large repository, run `git clone` via SSH (invoking `SSHUploadPackWithSidechannel`) or HTTP (`PostUploadPackWithSidechannel`) against a multi-GB repository.
2. Observe the `x-gitaly-cost` trailer returned: `computeCost` = `staticCostForMethod(...)` (fixed `10` from `staticCostOverrides`) + `dynamicCostFromContext(ctx)`.
3. Because the pack data is written to the sidechannel connection (see `sshUploadPack`/`packObjectsHook` code paths) and never passed through `grpc.StatsHandler`'s `InPayload`/`OutPayload` events, `grpcstats.PayloadBytesStatsFromContext(ctx)` is either `nil` or reflects only trivial in-band bytes, so `dynamicCostFromContext` returns `0` regardless of the multi-GB transfer.
4. Repeating the clone with a 10 KB repo vs. a 10 GB repo produces the identical `x-gitaly-cost: 10` trailer in both cases — confirmable directly from `internal/grpc/middleware/costhandler/costhandler_test.go`'s `TestComputeCost` ("static only when no bytes" case returns `10` even though the RPC is documented as serving arbitrary-size payloads) combined with the sidechannel data-path code in `internal/gitaly/service/ssh/upload_pack.go` (lines 131-158) which never touches `grpcstats.PayloadBytes`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L38-58)
```go
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

**File:** internal/grpc/grpcstats/stats.go (L26-36)
```go
// HandleRPC implements per-RPC tracing and stats instrumentation.
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

**File:** internal/gitaly/service/ssh/upload_pack.go (L131-158)
```go
func (s *server) SSHUploadPackWithSidechannel(ctx context.Context, req *gitalypb.SSHUploadPackWithSidechannelRequest) (*gitalypb.SSHUploadPackWithSidechannelResponse, error) {
	conn, err := sidechannel.OpenSidechannel(ctx)
	if err != nil {
		return nil, structerr.NewInternal("opennig sidechannel: %w", err)
	}
	defer conn.Close()

	// Count all bytes written back to the client so we can report the
	// client-facing transfer size, labeled by clone type.
	counter := &helper.CountingWriter{W: conn}
	sidebandWriter := pktline.NewSidebandWriter(counter)
	stdout := sidebandWriter.Writer(stream.BandStdout)
	stderr := sidebandWriter.Writer(stream.BandStderr)
	stats, _, err := s.sshUploadPack(ctx, req, conn, stdout, stderr)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	s.recordServedBytes(stats, counter.N)

	if err := conn.Close(); err != nil {
		return nil, structerr.NewInternal("close sidechannel: %w", err)
	}

	return &gitalypb.SSHUploadPackWithSidechannelResponse{
		PackfileNegotiationStatistics: stats.ToProto(),
	}, nil
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

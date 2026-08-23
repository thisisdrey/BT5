### Title
Sidechannel bulk data transfer bypasses gRPC payload-byte accounting, causing `x-gitaly-cost` to systematically understate the true resource cost of fetch/clone RPCs - (File: `internal/grpc/middleware/costhandler/costhandler.go`)

### Summary
The `costhandler` package computes a per-RPC cost score (`x-gitaly-cost` trailer) that Rails/Workhorse forward as `X-Score` to Cloudflare's complexity-based rate limiter. The score is `static + dynamic`, where the dynamic component is derived from bytes tracked by the `grpcstats.PayloadBytes` gRPC stats handler. However, `PostUploadPackWithSidechannel` and `SSHUploadPackWithSidechannel` — the RPCs used for ordinary `git fetch`/`git clone` — transfer the actual packfile bytes over a raw Yamux sidechannel stream that is completely outside the gRPC/Protobuf message path, and therefore invisible to `grpcstats.PayloadBytes`. This is analogous to the reported SponsorVault flaw, where the subsidy/accounting mechanism computed reimbursement from a nominal/self-reported value rather than the true economic size of the transfer, letting an actor extract value disproportionate to the real cost incurred.

### Finding Description
`computeCost` combines a static per-method weight with a dynamic component read from context: [1](#0-0) 

The dynamic component is sourced exclusively from `grpcstats.PayloadBytesStatsFromContext`, which is populated by the `PayloadBytes` `stats.Handler` hooked into the gRPC server: [2](#0-1) [3](#0-2) 

This stats handler only observes `stats.InPayload`/`stats.OutPayload` events, which gRPC emits for Protobuf messages sent over the gRPC stream itself. But `PostUploadPackWithSidechannel` deliberately moves the bulk packfile transfer to a side-channel byte stream ("Stream 3") multiplexed over the same TCP connection via Yamux, specifically to avoid gRPC/Protobuf message overhead: [4](#0-3) [5](#0-4) 

The handler implementation confirms the sidechannel connection (`conn`) is used directly as stdin/stdout for `git-upload-pack`, and the gRPC response is an essentially empty message (just negotiation stats): [6](#0-5) [7](#0-6) 

Because the pack data (which can be gigabytes for a large repository clone) never crosses the gRPC `InPayload`/`OutPayload` stats path, `dynamicCostFromContext` always sees near-zero bytes for these RPCs, and the score falls back to the flat static override: [8](#0-7) 

So a 10 KB fetch and a 50 GB fetch of the same repository via `PostUploadPackWithSidechannel` both report the same fixed cost of `10`, verified by the existing test which shows the cost is static-only regardless of the sidechannel data volume actually served: [9](#0-8) 

### Impact Explanation
`x-gitaly-cost`/`X-Score` is explicitly documented as the mechanism by which Gitaly communicates real per-RPC resource cost to an upstream rate limiter (Cloudflare on GitLab.com): [10](#0-9) 

Because sidechannel-served bytes are invisible to the byte-based dynamic cost component, any ordinary authenticated user who repeatedly fetches/clones large repositories via the standard Git HTTP or SSH sidechannel path consumes disproportionately large bandwidth, CPU (pack generation), and I/O while being scored identically to trivial fetches. This lets such traffic evade the very complexity-based rate limiting the cost score exists to drive, undermining the "innermost ring of defense" DoS mitigation Gitaly is documented to provide, and enabling sustained resource exhaustion (bandwidth/CPU/disk contention) against the node without triggering the upstream throttling that legitimate accounting is meant to invoke.

### Likelihood Explanation
This requires no special privileges beyond being an ordinary authenticated Git user capable of running `git clone`/`git fetch` against a large repository — the exact path used for every normal fetch over HTTP or SSH sidechannel. No malicious peer, leaked token, or MITM condition is needed; the gap is a structural accounting blind spot that is always present for `PostUploadPackWithSidechannel` and `SSHUploadPackWithSidechannel`/`SSHUploadPack`.

### Recommendation
Track bytes actually transferred over sidechannel connections (e.g., wrap `sidechannel.ServerConn`/`ClientConn` read/write with a byte counter) and feed that count into the same `PayloadBytesStats`/context value that `costhandler.dynamicCostFromContext` reads, so the dynamic cost component reflects true bytes served regardless of whether they went through gRPC framing or a sidechannel. Alternatively, compute the dynamic cost component for sidechannel RPCs from the packfile negotiation/served-bytes metrics already collected server-side (e.g., the `servedBytesMetrics`/`PackfileNegotiationStatistics` used in tests) rather than relying solely on the gRPC stats handler.

### Proof of Concept
1. Configure Gitaly with the `costhandler` interceptor enabled (as in `internal/gitaly/server/server.go`).
2. Create two repositories: one trivially small, one containing several GB of blob data.
3. Issue `PostUploadPackWithSidechannel` fetches against both (equivalent to `git clone`), as exercised in the existing test harness: [11](#0-10) 
4. Inspect the `x-gitaly-cost` trailer returned for each call — both return the fixed static cost `10` (per `staticCostOverrides["/gitaly.SmartHTTPService/PostUploadPackWithSidechannel"]`), confirmed by `TestComputeCost`'s "no stats in context"/"static only when no bytes" cases returning `10` regardless of payload: [12](#0-11) 
5. Repeating the large-repository fetch many times in a loop generates significant real bandwidth/CPU load on the Gitaly node while contributing a constant, minimal score to any downstream `X-Score`-based rate limiter, demonstrating the accounting/rate-limit-evasion gap.

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L38-44)
```go
var staticCostOverrides = map[string]int{
	// Streaming RPCs that transfer large amounts of data.
	"/gitaly.SmartHTTPService/PostUploadPackWithSidechannel": 10,
	"/gitaly.SmartHTTPService/PostReceivePack":               10,
	"/gitaly.SSHService/SSHUploadPack":                       10,
	"/gitaly.SSHService/SSHUploadPackWithSidechannel":        10,
	"/gitaly.SSHService/SSHReceivePack":                      10,
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

**File:** doc/sidechannel.md (L9-18)
```markdown
Prior to sidechannel, the only way for Gitaly to serve a byte stream
was to encapsulate the bytes in gRPC Protobuf messages. Because of the
per-message overhead, this acted as a limiting factor on how much Git
fetch traffic a Gitaly server could serve up.

The sidechannel protocol works around this by:

1. Allowing the Gitaly server to establish a sidechannel to the Gitaly client during an RPC call.
1. Performing the bulk data transfer on the sidechannel.

```

**File:** doc/sidechannel.md (L96-103)
```markdown
### Sidechannel Usage

When an RPC needs to transfer bulk data (e.g., `PostUploadPackWithSidechannel`):

1. The server opens a **new short-lived Yamux stream**
1. Bulk data flows over this stream without gRPC/Protobuf overhead
1. The stream closes when the data transfer completes
1. The gRPC call returns with status and metadata
```

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L21-52)
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

	if err := conn.Close(); err != nil {
		return nil, structerr.NewInternal("close sidechannel connection: %w", err)
	}

	return &gitalypb.PostUploadPackWithSidechannelResponse{
		PackfileNegotiationStatistics: stats.ToProto(),
	}, nil
}
```

**File:** proto/go/gitalypb/smarthttp.pb.go (L200-209)
```go
// PostUploadPackWithSidechannelResponse is the response for the PostUploadPackWithSidechannel rpc.
// This is an empty response since the raw data is transferred to the client via the sidechannel
// exclusively.
type PostUploadPackWithSidechannelResponse struct {
	state protoimpl.MessageState `protogen:"open.v1"`
	// packfile_negotiation_statistics is the packfile negotiation statistics.
	PackfileNegotiationStatistics *PackfileNegotiationStatistics `protobuf:"bytes,1,opt,name=packfile_negotiation_statistics,json=packfileNegotiationStatistics,proto3" json:"packfile_negotiation_statistics,omitempty"`
	unknownFields                 protoimpl.UnknownFields
	sizeCache                     protoimpl.SizeCache
}
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

**File:** doc/load-management-architecture.md (L271-280)
```markdown
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

**File:** internal/gitaly/service/smarthttp/upload_pack_test.go (L868-917)
```go
func makePostUploadPackWithSidechannelRequest(t *testing.T, ctx context.Context, serverSocketPath, token string, in *gitalypb.PostUploadPackWithSidechannelRequest, body io.Reader) (*bytes.Buffer, error) {
	t.Helper()

	registry := sidechannel.NewRegistry()
	conn := dialSmartHTTPServerWithSidechannel(t, serverSocketPath, token, registry)
	client := gitalypb.NewSmartHTTPServiceClient(conn)
	defer testhelper.MustClose(t, conn)

	responseBuffer := &bytes.Buffer{}
	ctxOut, waiter := sidechannel.RegisterSidechannel(ctx, registry, func(sideConn *sidechannel.ClientConn) error {
		var wg sync.WaitGroup
		defer wg.Wait()

		wg.Add(1)
		errC := make(chan error, 1)
		go func() {
			defer wg.Done()
			_, err := io.Copy(responseBuffer, sideConn)
			errC <- err
		}()

		if body != nil {
			if _, err := io.Copy(sideConn, body); err != nil {
				return err
			}
		}

		if err := sideConn.CloseWrite(); err != nil {
			return err
		}

		return <-errC
	})

	rpcRequest := &gitalypb.PostUploadPackWithSidechannelRequest{
		Repository:       in.GetRepository(),
		GitConfigOptions: in.GetGitConfigOptions(),
		GitProtocol:      in.GetGitProtocol(),
	}
	_, err := client.PostUploadPackWithSidechannel(ctxOut, rpcRequest)
	if err == nil {
		testhelper.MustClose(t, waiter)
	} else if err := waiter.Close(); err != nil && !errors.Is(err, sidechannel.ErrCallbackDidNotRun) {
		// When the request failed the sidechannel may not even have been used, so we need
		// to catch the `ErrCallbackDidNotRun` error here.
		require.NoError(t, err)
	}

	return responseBuffer, err
}
```

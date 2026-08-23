Confirmed: `PostUploadPackWithSidechannel` transfers its entire pack-file payload over a raw sidechannel connection opened via `sidechannel.OpenSidechannel`, completely outside the gRPC message stream. The gRPC unary call itself only carries the small `PostUploadPackWithSidechannelRequest`/`Response` messages.

### Title
Cost-metering for cost-accounting undercounts sidechannel-transferred bytes, causing rate-limit/DoS discrepancy - ([File: internal/grpc/middleware/costhandler/costhandler.go])

### Summary
The `costhandler` package computes a per-RPC "cost score" (`x-gitaly-cost` trailer) that GitLab Rails/Workhorse/Cloudflare use as a resource-consumption budget signal, analogous to gas metering in the reported issue. The dynamic component of this score is derived exclusively from `grpcstats.PayloadBytesStats`, which is populated by the gRPC `stats.Handler` from `stats.InPayload`/`stats.OutPayload` events — i.e. only bytes that pass through gRPC message frames.

### Finding Description
`dynamicCostFromContext` sums `InPayloadBytes + OutPayloadBytes` from the context and divides by `byteCostDivisor` (1 MiB) to derive the dynamic cost component [1](#0-0) . This is added to a small static cost per RPC type, with `PostUploadPackWithSidechannel` given only a fixed static override of `10` [2](#0-1) .

However, `PostUploadPackWithSidechannel` does not send its (potentially gigabyte-scale) pack-file response through the gRPC stream at all. It opens a raw side-channel socket via `sidechannel.OpenSidechannel`, and all upload-pack output is streamed directly over that connection with `io.CopyBuffer(stdout, cmd, ...)` [3](#0-2) [4](#0-3) . Because this data bypasses gRPC message framing, it is never observed by `grpcstats.PayloadBytes.HandleRPC`, which only reacts to `stats.InPayload`/`stats.OutPayload` events [5](#0-4) . Consequently `dynamicCostFromContext` always returns `0` for this RPC regardless of how many bytes of packfile data were actually served, and the emitted `x-gitaly-cost` trailer is just the fixed static value `10`, independent of the true response size (e.g. a 10 GiB clone and a nearly-empty clone are billed identically).

This mirrors the reported CrossDomainMessenger bug pattern precisely: a metering function computes "gas"/"cost" over only part of the total data actually transmitted/processed (the "wrapped" bulk transfer, analogous to the `relayMessage` wrapper, is excluded), so downstream consumers of that metric (Rails' rate-limit budget accounting, Cloudflare's `X-Score` header, per `doc/load-management-architecture.md` referenced in the package docstring [6](#0-5) ) systematically underestimate resource consumption for exactly the RPC class that transfers the most data.

### Impact Explanation
Any unprivileged client (or an already-authenticated Git client performing ordinary `git clone`/`git fetch`) that triggers `PostUploadPackWithSidechannel` will always be assigned a flat, low cost score regardless of the actual bytes served through `git-upload-pack`. Since this score directly feeds an external rate-limit/load-shedding budget mechanism, an attacker can perform repeated large clones/fetches while being metered as if each were a trivial, fixed-cost operation, undermining the very DoS/resource-limit protection the mechanism is meant to enforce (`RPC-handler resource limits` is explicitly an in-scope impact category).

### Likelihood Explanation
High: `PostUploadPackWithSidechannel` is the primary path for regular Git clone/fetch over SSH/HTTP with sidechannel support and is reachable by any user with fetch access to a repository — no privileged actor, leaked token, or malicious peer is required. The undercounting is deterministic and occurs on every single invocation of this RPC.

### Recommendation
Include the number of bytes actually copied through the sidechannel (`respBytes` returned by `runUploadPack`, already computed at [7](#0-6) ) in the dynamic cost calculation for `PostUploadPackWithSidechannel` (and any other sidechannel-based RPC), e.g. by threading the served-byte count into the `grpcstats.PayloadBytesStats` in context, or by having `costhandler.computeCost` consult an RPC-specific byte counter that also accounts for sidechannel traffic, rather than relying solely on gRPC's `InPayload`/`OutPayload` stats events.

### Proof of Concept
1. As an ordinary user with read access to a repository, invoke `SSHUploadPackWithSidechannel`/`PostUploadPackWithSidechannel` to clone a large repository (e.g. several GiB of pack data).
2. Observe the `x-gitaly-cost` trailer returned by Gitaly: it equals the fixed static override (`10`) plus `Ceil((InPayloadBytes+OutPayloadBytes)/1MiB)`, where `InPayloadBytes`/`OutPayloadBytes` reflect only the tiny `PostUploadPackWithSidechannelRequest`/`Response` gRPC messages, not the actual multi-gigabyte packfile transferred over the sidechannel socket (confirmed by tracing `runUploadPack`'s `io.CopyBuffer(stdout, cmd, ...)` writing directly to the sidechannel connection, bypassing the gRPC stream entirely).
3. Repeat the large clone many times; each is scored identically to a near-empty clone, demonstrating the cost/resource-consumption discrepancy that a downstream rate-limiter would fail to detect.

### Citations

**File:** internal/grpc/middleware/costhandler/costhandler.go (L1-9)
```go
// Package costhandler is a Signal Export layer component that reads per-RPC
// resource data and sets the x-gitaly-cost gRPC response trailer.
//
// The cost score combines a static weight for the RPC type with the dynamic
// bytes transferred. Clients (Rails, Workhorse) use this for rate-limit
// budget accounting and forward it as the X-Score HTTP header for Cloudflare.
//
// See doc/load-management-architecture.md for the full design.
package costhandler
```

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

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L127-183)
```go
func (s *server) runUploadPack(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest, repoPath string, gitConfig []gitcmd.ConfigPair, stdin io.Reader, stdout io.Writer) (stats *stats.PackfileNegotiation, _ error) {
	h := sha1.New()

	stdin = io.TeeReader(stdin, h)
	stdin, collector := s.runStatsCollector(ctx, stdin)
	defer func() {
		if stats == nil {
			stats = collector.finish()
		}
	}()

	repo := s.localRepoFactory.Build(req.GetRepository())
	if s.bundleURIManager != nil {
		// Bundle generation is an optimization that is transparent to users.
		// If it fails, we log the error but continue with the regular upload-pack
		// operation without the bundle optimization.
		// If successful, a goroutine is spawned to generate the bundle, in which case
		// the bundle generation becomes independent of the RPC request.
		if err := s.bundleURIManager.GenerateWithStrategy(ctx, repo); err != nil {
			s.logger.WithError(err).Error("failed generating bundle")
		}
		gitConfig = append(gitConfig, s.bundleURIManager.UploadPackGitConfig(ctx, req.GetRepository())...)
	} else {
		gitConfig = append(gitConfig, bundleuri.CapabilitiesGitConfig(ctx, false)...)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

	commandOpts := []gitcmd.CmdOpt{
		gitcmd.WithStdin(stdin),
		gitcmd.WithSetupStdout(),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(gitConfig...),
		gitcmd.WithPackObjectsHookEnv(objectHash, req.GetRepository(), "http"),
	}

	if s.cfg.Hooks.PackObjectsHookMaxProc > 0 {
		commandOpts = append(commandOpts, gitcmd.WithEnv("GOMAXPROCS="+strconv.Itoa(int(s.cfg.Hooks.PackObjectsHookMaxProc))))
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name:  "upload-pack",
		Flags: []gitcmd.Option{gitcmd.Flag{Name: "--stateless-rpc"}},
		Args:  []string{repoPath},
	}, commandOpts...)
	if err != nil {
		return nil, structerr.NewFailedPrecondition("spawning upload-pack: %w", err)
	}

	// Use a custom buffer size to minimize the number of system calls.
	respBytes, err := io.CopyBuffer(stdout, cmd, make([]byte, 64*1024))
	if err != nil {
		return nil, structerr.NewFailedPrecondition("copying stdout from upload-pack: %w", err)
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

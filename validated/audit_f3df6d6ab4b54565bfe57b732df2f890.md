### Title
Client-triggered error-threshold circuit breaker allows any authenticated user to mark a healthy Gitaly node unhealthy, causing cluster-wide denial of service - (File: internal/praefect/nodes/tracker/errors.go, internal/praefect/middleware/errorhandler.go, internal/praefect/nodes/tracker/health_client.go)

### Summary
The Nouns Builder bug is a case where an unprivileged caller can steer an error-handling branch (a `catch` block) into a state-changing action (`_pause()`) that impacts every user of the contract, simply by causing the "unhappy path" to be taken under attacker-controlled conditions. The closest reachable analog in this Gitaly snapshot is the read/write error-threshold circuit breaker used by Praefect's node health tracking: any RPC error (including ones triggerable by an ordinary, authenticated client through normal/malformed-but-authorized requests) increments a shared per-node error counter, and once a threshold is crossed within a time window, `HealthClient.Check` starts unconditionally failing health checks for that storage node — independent of whether the node is actually healthy.

### Finding Description
`errorTracker.IncrReadErr` / `IncrWriteErr` (`internal/praefect/nodes/tracker/errors.go:76-116`) record a timestamp per RPC error for a given node storage, and `ReadThresholdReached` / `WriteThresholdReached` (`errors.go:118-164`) report whether the configured threshold was exceeded within the configured window. [1](#0-0) 

These counters are fed from `catchErrorStreamer.SendMsg`/`RecvMsg` in `internal/praefect/middleware/errorhandler.go`, which increments the tracker on **any** non-`io.EOF` error returned by a proxied RPC to a Gitaly node, regardless of whether the error was caused by a genuine node problem or simply a client-supplied invalid/edge-case request (e.g. a nil repository field, as shown in the test). [2](#0-1) [3](#0-2) 

Once the threshold is reached, `HealthClient.Check` (`internal/praefect/nodes/tracker/health_client.go:28-39`) short-circuits and returns an error for every subsequent health check against that storage — effectively acting as a "pause" for that node from Praefect's point of view, independent of the node's real availability. [4](#0-3) 

The consuming code in `internal/praefect/nodes/manager.go` treats a failed `CheckHealth` as evidence of an unhealthy node, which is used by the (non–per-repository) failover/election path to route traffic away from that node — see `TestErrorThreshold` in `internal/praefect/server_test.go`, which demonstrates exactly this: after 5 induced errors via `bad-header` requests, the node is reported unhealthy even though `node.CheckHealth` itself never failed. [5](#0-4) 

This mirrors the structure of the reported bug: a resource/error condition that a caller can deliberately and repeatedly trigger (analogous to consuming just the right amount of gas) drives an error-handling counter past a threshold, and crossing that threshold flips global state (health status) that affects all other tenants of the shared node — not just the caller who triggered the errors.

### Impact Explanation
If an authenticated but otherwise unprivileged client can cause enough RPC errors against a given storage node within the configured `ErrorThresholdWindow` (default read/write thresholds are operator-configured, but nothing prevents a client from generating errors deliberately — e.g., repeatedly calling accessor/mutator RPCs with malformed fields, non-existent repositories, or other easily-triggered error paths), it can flip that node to "unhealthy" from Praefect's perspective. In the legacy (non-per-repository) election strategy this can force a failover or make the virtual storage unavailable for all repositories served by that node, denying service to every other client — a cluster-wide DoS triggered by a single low-privilege actor, analogous to the Medium-severity "malicious pausing" finding.

### Likelihood Explanation
Likelihood is moderate to low depending on deployment. It requires: (1) the legacy node-manager/election strategy (not per-repository elector) to be in use, since the health/error-threshold tracker is wired into that manager; (2) sufilcient RPC error attempts within the configured window to exceed the configured `read`/`write` thresholds, which are operator-tunable and could be small in some configurations; (3) network/auth access sufficient to send RPCs at all (any authenticated Gitaly/Praefect client). Unlike the original gas-timing bug, this doesn't require precise resource calibration — simply repeating a cheap, reliably-erroring RPC call is enough, which arguably makes it *easier* to trigger than the original finding, though the blast radius depends on cluster topology and whether other healthy secondaries exist to absorb failover.

### Recommendation
- Do not let the error-threshold circuit breaker mix client-induced application errors (invalid arguments, not-found, etc.) with actual node/infrastructure failures. Only increment the tracker for errors that indicate genuine node unavailability (e.g. `Unavailable`, transport-level failures), not for client-caused `InvalidArgument`/`NotFound`-style errors.
- Scope error tracking (or at least rate-limit it) per calling identity/repository rather than purely per destination storage, so that one caller cannot单-handedly degrade the shared health signal for an entire node.
- Add hysteresis/verification: before acting on a threshold-triggered "unhealthy" signal (e.g., initiating failover), corroborate with an independent, error-type-filtered health probe rather than short-circuiting `HealthClient.Check` purely from the error-count side channel.

### Proof of Concept
Based on the existing test `TestErrorThreshold` (`internal/praefect/server_test.go:1017-1106`): a client repeatedly sends RPCs with a bad header/invalid field (e.g., `RepositoryExists`/`WriteRef` with `Repository: nil` as shown in `errorhandler_test.go:118-127`) against a healthy node until the configured read or write error threshold is exceeded within the error window; subsequent `node.CheckHealth` calls then report the node as unhealthy (`server_test.go:1101-1103`) even though the node's actual health-check RPC never failed, demonstrating that a client can flip Praefect's health view of a node purely through induced application-level errors. [5](#0-4)

### Citations

**File:** internal/praefect/nodes/tracker/errors.go (L76-95)
```go
// IncrReadErr increases the read errors for a node by 1
func (e *errorTracker) IncrReadErr(node string) {
	e.incrReadErrTime(node, time.Now())
}

func (e *errorTracker) incrReadErrTime(node string, t time.Time) {
	select {
	case <-e.ctx.Done():
		return
	default:
		e.m.Lock()
		defer e.m.Unlock()

		e.readErrors[node] = append(e.readErrors[node], t)

		if len(e.readErrors[node]) > e.readThreshold {
			e.readErrors[node] = e.readErrors[node][1:]
		}
	}
}
```

**File:** internal/praefect/middleware/errorhandler.go (L46-59)
```go
// SendMsg proxies the send but records any errors
func (c *catchErrorStreamer) SendMsg(m interface{}) error {
	err := c.ClientStream.SendMsg(m)
	if err != nil {
		switch c.operation {
		case protoregistry.OpAccessor:
			c.errors.IncrReadErr(c.nodeStorage)
		case protoregistry.OpMutator:
			c.errors.IncrWriteErr(c.nodeStorage)
		}
	}

	return err
}
```

**File:** internal/praefect/middleware/errorhandler_test.go (L104-130)
```go
	for i := 0; i < threshold; i++ {
		_, err = client.RepositoryExists(ctx, &gitalypb.RepositoryExistsRequest{
			Repository: repo,
		})
		require.NoError(t, err)
		_, err = client.WriteRef(ctx, &gitalypb.WriteRefRequest{
			Repository: repo,
		})
		require.NoError(t, err)
	}

	assert.False(t, errTracker.WriteThresholdReached(nodeName))
	assert.False(t, errTracker.ReadThresholdReached(nodeName))

	for i := 0; i < threshold; i++ {
		_, err = client.RepositoryExists(ctx, &gitalypb.RepositoryExistsRequest{
			Repository: nil,
		})
		require.Error(t, err)
		_, err = client.WriteRef(ctx, &gitalypb.WriteRefRequest{
			Repository: nil,
		})
		require.Error(t, err)
	}

	assert.True(t, errTracker.WriteThresholdReached(nodeName))
	assert.True(t, errTracker.ReadThresholdReached(nodeName))
```

**File:** internal/praefect/nodes/tracker/health_client.go (L28-39)
```go
// Check circuit breaks the health check if write or read error thresholds have been reached. If not, it performs
// the health check.
func (hc HealthClient) Check(ctx context.Context, req *grpc_health_v1.HealthCheckRequest, opts ...grpc.CallOption) (*grpc_health_v1.HealthCheckResponse, error) {
	if hc.tracker.ReadThresholdReached(hc.storage) {
		return nil, fmt.Errorf("read error threshold reached for storage %q", hc.storage)
	}

	if hc.tracker.WriteThresholdReached(hc.storage) {
		return nil, fmt.Errorf("write error threshold reached for storage %q", hc.storage)
	}

	return hc.HealthClient.Check(ctx, req, opts...)
```

**File:** internal/praefect/server_test.go (L1082-1104)
```go
			for i := 0; i < 5; i++ {
				ctx := metadata.AppendToOutgoingContext(ctx, "bad-header", "true")

				healthy, err := node.CheckHealth(ctx)
				require.NoError(t, err)
				require.True(t, healthy)

				if tc.accessor {
					_, err = cli.RepositoryExists(ctx, &gitalypb.RepositoryExistsRequest{
						Repository: repo,
					})
				} else {
					_, err = cli.ReplicateRepository(ctx, &gitalypb.ReplicateRepositoryRequest{
						Repository: repo,
					})
				}
				testhelper.RequireGrpcError(t, status.Error(codes.Internal, "something went wrong"), err)
			}

			healthy, err := node.CheckHealth(ctx)
			require.Equal(t, tc.error, err)
			require.False(t, healthy)
		})
```

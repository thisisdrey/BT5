Found a strong analog in the Raft replica event loop.

### Title
Raft replica event loop terminates permanently on transient transport errors, causing indefinite proposal hangs - (File: internal/gitaly/storage/raftmgr/replica.go)

### Summary
The single-node `run()` goroutine in `Replica` is structurally similar to the reported EVM channel task: it is a single long-running loop that processes all Raft state transitions for a partition, and any error it encounters — including transient, recoverable ones — causes the loop to `return` and die permanently via `handleFatalError`, exactly like the reported pattern of a task-terminating `return` instead of `continue`/retry.

### Finding Description
`(*Replica).run` is the sole consumer of `replica.node.Ready()`. On each `Ready` event it calls `handleReady`, and if that returns any error the loop calls `replica.handleFatalError(err)` and `return`s, permanently ending the goroutine for that partition's replica: [1](#0-0) 

`handleReady` propagates errors from `sendMessages`, which in turn calls `transport.Send(replica.ctx, replica, replica.partitionKey, rd.Messages)` to deliver Raft protocol messages to other cluster members over the network: [2](#0-1) 

Any network/transport error (dial failure, deadline exceeded, peer temporarily down) bubbles straight up through `sendMessages` → `handleReady` → `run`, unconditionally killing the replica's event loop, even though such errors are transient and unrelated to the correctness of the local WAL or Raft state machine — analogous to the reported `GetBalance`/`TokenSettleHTLC` cases where a single transient RPC failure terminated the whole channel task.

`handleFatalError` does not cancel `replica.ctx`; it only signals readiness error, untracks waiters, and best-effort pushes to `notifyQueue`: [3](#0-2) 

Because `replica.ctx` is not cancelled, any subsequent call to `AppendLogEntry` (the integration point used whenever a transaction is committed through this partition, i.e., on ordinary user pushes) will call `replica.node.Propose(ctx, data)` successfully (the raft library just buffers it internally) and then block on `select { case <-ctx.Done(): ... case err := <-w.C: ... }`. Since the `run()` goroutine that would read `node.Ready()` and eventually unblock `w.C` is dead, and `ctx` is only bounded by an optional `opTimeout` (which is described as "more useful in testing environments," implying it's often unset in production), the proposal — and therefore the RPC handling the write — hangs indefinitely or until an operator-configured timeout, exactly mirroring the reported plugin "always timeout" condition: [4](#0-3) 

### Impact Explanation
A single transient network blip while sending Raft protocol messages to a peer permanently disables consensus processing for that partition/repository. Every subsequent transaction proposal through `AppendLogEntry` for that partition will hang, causing all writes (and, transitively, RPCs waiting on quorum) to that repository/partition to stall — a denial of service reachable by any ordinary push once the affected partition is on the write path, with no automatic recovery since the goroutine is gone and nothing appears to restart it.

### Likelihood Explanation
This requires the Raft feature to be enabled with multi-node transport in use, and a transient network error while sending Raft messages between replicas. This is a normal, expected occurrence in distributed systems (temporary connectivity loss, node restart, brief network partition), so likelihood is meaningful in any multi-node Raft-enabled Gitaly Cluster deployment, though this code appears to be for an in-progress/experimental Raft feature area (comments reference "current single-node implementation" and open issues like #6304, #6463, #6465), which may limit current production exposure.

### Recommendation
Distinguish between fatal (state-corrupting, e.g., WAL write failures, unmarshalling failures) and transient (e.g., transport/network) errors in `handleReady`'s call chain. For transient errors such as those from `sendMessages`/`transport.Send`, log and retry/backoff rather than terminating the `run()` loop, mirroring the report's recommendation to replace loop-terminating `return`s with `continue`. Additionally, ensure `handleFatalError` cancels `replica.ctx` (or otherwise unblocks all pending/future `AppendLogEntry` calls) so that when the loop does have to die, callers fail fast with a clear error instead of hanging.

### Proof of Concept
1. Configure a multi-node Raft-enabled Gitaly Cluster partition with at least two replicas.
2. Induce a transient network failure between the leader and a follower (e.g., drop/delay packets, restart the peer briefly) at the moment the leader's `run()` loop calls `sendMessages` → `transport.Send`.
3. Observe that `transport.Send` returns an error, which propagates to `handleReady`, causing `run()` to call `handleFatalError` and exit permanently for that replica.
4. Issue an ordinary user push targeting that partition; the write path invokes `AppendLogEntry`, which calls `node.Propose` successfully but then blocks forever on `w.C`/`ctx.Done()` because no `opTimeout` is configured and `replica.ctx` was never cancelled.
5. Confirm the RPC hangs indefinitely (or until any operator-level gRPC deadline) and that the partition never recovers without a manual Gitaly process restart, since nothing re-invokes `Initialize`/`run` for the dead replica.

### Citations

**File:** internal/gitaly/storage/raftmgr/replica.go (L490-504)
```go
		case rd, ok := <-replica.node.Ready():
			if err := replica.safeExec(func() error {
				if !ok {
					return fmt.Errorf("raft node Ready channel unexpectedly closed")
				}
				if err := replica.handleReady(&rd); err != nil {
					return err
				}
				replica.hooks.BeforeAdvance()
				replica.node.Advance()
				return nil
			}); err != nil {
				replica.handleFatalError(err)
				return
			}
```

**File:** internal/gitaly/storage/raftmgr/replica.go (L550-567)
```go
// handleFatalError handles a fatal error that requires the run loop to terminate
func (replica *Replica) handleFatalError(err error) {
	// Set back to ready to unlock the caller of Initialize().
	replica.signalError(ErrReplicaStopped)
	// Unlock all waiters of AppendLogEntry about the replica being stopped.
	replica.registry.UntrackAll(ErrReplicaStopped)
	replica.metrics.eventLoopCrashes.Inc()

	replica.logger.WithError(err).Error("raft event loop failed")
	// Ensure the error is sent to the notification queue. The queue is buffered but may
	// already be full and have no consumer if the replica is being closed concurrently.
	// Bail out on context cancellation so the run loop can return and Close() can complete
	// its wg.Wait() instead of deadlocking on this send.
	select {
	case replica.notifyQueue <- err:
	case <-replica.ctx.Done():
	}
}
```

**File:** internal/gitaly/storage/raftmgr/replica.go (L686-705)
```go
	// Set an optional timeout to prevent proposal processing takes forever. This option is
	// more useful in testing environments.
	if replica.options.opTimeout != 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(replica.ctx, replica.options.opTimeout)
		defer cancel()
	}

	replica.hooks.BeforePropose(logEntryPath)
	if err := replica.node.Propose(ctx, data); err != nil {
		return 0, fmt.Errorf("proposing Raft message: %w", err)
	}

	select {
	case <-ctx.Done():
		return 0, ctx.Err()
	case err := <-w.C:
		return w.LSN, err
	}
}
```

**File:** internal/gitaly/storage/raftmgr/replica.go (L970-989)
```go
// sendMessages delivers pending Raft messages to other members via the transport layer.
// This function is responsible for sending Raft protocol messages between members.
func (replica *Replica) sendMessages(rd *raft.Ready) error {
	replica.hooks.BeforeSendMessages()
	if len(rd.Messages) > 0 {
		// To mitigate the "chatty" nature of the Raft protocol, Gitaly will implement
		// techniques such as batching health checks and quiescing inactive groups.
		//
		// See https://gitlab.com/gitlab-org/gitaly/-/issues/6304
		transport := replica.raftEnabledStorage.GetTransport()
		if transport == nil {
			return fmt.Errorf("transport not found")
		}
		err := transport.Send(replica.ctx, replica, replica.partitionKey, rd.Messages)
		if err != nil {
			return err
		}
	}
	return nil
}
```

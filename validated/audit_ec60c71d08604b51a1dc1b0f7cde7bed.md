### Title
Global QUIC connection-slot exhaustion via indefinite stream-timeout looping - (streamer/src/nonblocking/quic.rs)

### Summary
An unprivileged remote client can hold a QUIC connection open indefinitely by repeatedly opening new unidirectional streams that never finish (each timing out via `wait_for_chunk_timeout`), because a stream-read timeout only aborts the *current stream*, not the connection. Doing this from enough source IPs lets an attacker permanently occupy slots counted by the single global `ClientConnectionTracker` counter, causing legitimate senders to be refused at accept time.

### Finding Description
`run_server` gates every accepted QUIC connection through a single global counter before any per-peer/per-stake logic runs: [1](#0-0) 

This counter is only released when the `ClientConnectionTracker` (held inside the per-connection `ConnectionEntry`) is dropped:

<cite repo="Kohvert/agave--029" path="streamer/src/nonblocking/quic.rs" start="229="234" end="252" />

The `ConnectionEntry`/tracker is only dropped when `handle_connection`'s outer `'conn` loop terminates — i.e. when `connection.accept_uni()` errors (peer actually disconnects) or the connection is cancelled/pruned: [2](#0-1) 

Critically, the inner per-stream read loop's timeout only `break`s the inner loop, returning control to the *outer* loop to accept the next stream on the same connection — it does not close the connection or drop the tracker: [3](#0-2) [4](#0-3) 

So a client can: open a uni stream, send nothing (or trickle bytes below `max_stream_data_bytes`), let `wait_for_chunk_timeout` fire, then immediately open a new stream — repeating forever. Each stream-open/trickle is enough QUIC traffic to also reset the transport-level `max_idle_timeout` configured in `configure_server`, so the connection is never force-closed by QUIC itself: [5](#0-4) 

The per-table LRU pruning used by `SwQos`/`SimpleQos` (`prune_unstaked_connection_table`, evicting the 10% oldest entries by `last_update`) does not help here, because `last_update` is refreshed on stream accept, not on completed/productive work: [6](#0-5) 

Since each new stream open is itself an activity event, the attacker's connection continuously looks "fresh" and is never the pruning candidate, while it permanently occupies one slot of the global `qos.max_concurrent_connections()` budget checked in `ClientConnectionTracker::new`.

### Impact Explanation
By repeating this loop across enough source IPs/connections (bounded only by the per-IP connection-attempt rate limiter and per-peer connection caps, not by number of distinct attacker-controlled IPs), an attacker can drive `stats.open_connections` up to `qos.max_concurrent_connections()` with connections that never submit a valid transaction. Once the global cap is saturated, every subsequent incoming connection — staked or unstaked, legitimate or not — is refused at `incoming.refuse()` before QoS/stake-based admission logic even runs, denying TPU access to all senders. This matches the "QoS evasion" / resource-exhaustion bounty category: the global connection-slot pool is not bounded relative to actual useful throughput, only to raw connection-open events.

### Likelihood Explanation
Fully reachable by an unprivileged unstaked client using only standard QUIC primitives (`open_uni`, partial/no writes, timed sleeps). No stake, keys, or special privileges required. The attack is cheap (near-zero bytes per stream) and trivially repeatable/scriptable; the only friction is the per-IP connection-rate limiter and per-peer connection cap, which just means the attacker needs multiple source IPs to reach the full global cap — feasible for a botnet or cloud-based attacker but even a handful of IPs can consume a meaningful fraction of `max_concurrent_connections`.

### Recommendation
Enforce a connection-level (not just stream-level) liveness/productivity timeout: track time since the connection last *completed* a valid stream (not merely accepted one), and force-close/evict connections that exceed a bounded number of consecutive stream-read timeouts or a bounded "connection age without a finished stream." Additionally, make LRU pruning eligibility based on last *successful* packet delivery rather than last stream-accept event, so idle/abusive connections become prunable candidates instead of perpetually "fresh."

### Proof of Concept
Integration test sketch (extending existing `test_quic_stream_timeout` style tests in `streamer/src/nonblocking/quic.rs`):
1. Spin up `setup_quic_server` with a small `max_concurrent_connections` (e.g. via a QoS config) and default `wait_for_chunk_timeout`.
2. From a single (or a few) client endpoints, open `qos.max_concurrent_connections()` connections; on each, loop: `open_uni()`, write 0 or 1 byte, sleep `wait_for_chunk_timeout - epsilon`, drop the stream (or let it be abandoned), `open_uni()` again — repeated well past several `wait_for_chunk_timeout` windows.
3. Assert `stats.open_connections` stays pinned at `max_concurrent_connections` and does not decrease across many timeout cycles (`stats.total_stream_read_timeouts` increments repeatedly, but `stats.connection_removed` does not).
4. Attempt one additional legitimate connection and assert it is refused (`stats.refused_connections_too_many_open_connections` increments) even though no attacker connection ever delivered a valid transaction — demonstrating the QoS/global-slot bypass.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L371-379)
```rust
            let Ok(client_connection_tracker) =
                ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())
            else {
                stats
                    .refused_connections_too_many_open_connections
                    .fetch_add(1, Ordering::Relaxed);
                incoming.refuse();
                continue;
            };
```

**File:** streamer/src/nonblocking/quic.rs (L610-623)
```rust
    'conn: loop {
        // Wait for new streams. If the peer is disconnected we get a cancellation signal and stop
        // the connection task.
        let mut stream = select! {
            stream = connection.accept_uni() => match stream {
                Ok(stream) => stream,
                Err(e) => {
                    debug!("stream error: {e:?}");
                    break;
                }
            },
            _ = cancel.cancelled() => break,
        };

```

**File:** streamer/src/nonblocking/quic.rs (L647-677)
```rust
        loop {
            // Read the next chunks, waiting up to `wait_for_chunk_timeout`. If we don't get chunks
            // before then, we assume the stream is dead. This can only happen if there's severe
            // packet loss or the peer stops sending for whatever reason.
            let n_chunks = match tokio::select! {
                chunk = tokio::time::timeout(
                    wait_for_chunk_timeout,
                    stream.read_chunks(&mut chunks)) => chunk,

                // If the peer gets disconnected stop the task right away.
                _ = cancel.cancelled() => break,
            } {
                // read_chunk returned success
                Ok(Ok(chunk)) => chunk.unwrap_or(0),
                // read_chunk returned error
                Ok(Err(e)) => {
                    debug!("Received stream error: {e:?}");
                    stats
                        .total_stream_read_errors
                        .fetch_add(1, Ordering::Relaxed);
                    break;
                }
                // timeout elapsed
                Err(_) => {
                    debug!("Timeout in receiving on stream");
                    stats
                        .total_stream_read_timeouts
                        .fetch_add(1, Ordering::Relaxed);
                    break;
                }
            };
```

**File:** streamer/src/nonblocking/quic.rs (L709-711)
```rust
        stats.active_streams.fetch_sub(1, Ordering::Relaxed);
        qos.on_stream_closed(&context);
    }
```

**File:** streamer/src/quic.rs (L119-120)
```rust
    let timeout = IdleTimeout::try_from(QUIC_MAX_TIMEOUT).unwrap();
    config.max_idle_timeout(Some(timeout));
```

**File:** streamer/src/nonblocking/swqos.rs (L241-256)
```rust
    fn prune_unstaked_connection_table(
        &self,
        unstaked_connection_table: &mut ConnectionTable<ConnectionStreamCounter>,
        max_unstaked_connections: usize,
        stats: Arc<StreamerStats>,
    ) {
        if unstaked_connection_table.total_size >= max_unstaked_connections {
            // Prune the connection table down to 90% capacity
            const PRUNE_TABLE_RATIO: f64 = 0.90;
            let max_connections = (PRUNE_TABLE_RATIO * (max_unstaked_connections as f64)) as usize;
            let num_pruned = unstaked_connection_table.prune_oldest(max_connections);
            stats
                .num_evictions_unstaked
                .fetch_add(num_pruned, Ordering::Relaxed);
        }
    }
```

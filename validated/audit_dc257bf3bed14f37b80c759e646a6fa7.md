### Title
Unstaked attacker can monopolize the shared `ClientConnectionTracker` headroom pool, starving legitimate handshakes - ([File: streamer/src/nonblocking/quic.rs])

### Finding Description
`ClientConnectionTracker::new` gates *every* incoming QUIC connection attempt (before peer classification) against a single shared ceiling, `qos.max_concurrent_connections()`, which for `SwQos` is `(max_staked_connections + max_unstaked_connections) * 5 / 4` — i.e., only a flat 25% headroom over the combined staked+unstaked budget, shared indiscriminately across all peer types. [1](#0-0) [2](#0-1) 

A tracker slot is reserved in `run_server`'s accept loop at the moment `incoming` passes the overall and per-IP rate limiters — well before the handshake completes and before QoS admission decides whether the connection is staked/unstaked or should be rejected: [3](#0-2) 

The tracker is then carried through `setup_connection`, which waits up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` for the handshake, then calls `qos.try_add_connection`, which can reject the connection (e.g., unstaked connection when `max_connections_per_unstaked_peer`/`max_unstaked_connections` capacity is exhausted, or `try_add_connection` failing after pruning attempts): [4](#0-3) [5](#0-4) 

Only when the `ClientConnectionTracker` value is dropped (which happens implicitly at the end of the rejecting function call, since it's never moved into a stored `ConnectionEntry` on the rejection path) does `stats.open_connections` decrement: [6](#0-5) [7](#0-6) 

Because the gate at `ClientConnectionTracker::new` doesn't distinguish staked vs. unstaked and doesn't reserve any portion of the pool for staked peers, an unstaked attacker who deliberately triggers rejection in `try_add_connection` (e.g., by exceeding the unstaked cap with many concurrent connections, or triggering per-peer connection limits) can hold slots in this shared pool for the full duration of the handshake+admission round trip. While those slots are occupied, any new incoming connection — including from staked/legitimate peers — is refused at `ClientConnectionTracker::new` with `refused_connections_too_many_open_connections`, before peer-type-based prioritization logic in `try_add_connection` ever runs. [3](#0-2) 

Existing mitigations before the tracker check are the overall rate limiter (`overall_connection_rate_limiter`, a global token bucket) and per-IP rate limiter (`ConnectionRateLimiter`) checked in the accept loop: [8](#0-7) 
These bound the *rate* of new tracker reservations from a single attacker IP but do not bound the number of *distinct* attacker-controlled source IPs (the per-IP limiter is keyed by IP and only limits repeated attempts from the same address, not from many different unstaked clients/IPs), nor do they reserve any portion of the shared pool specifically for staked peers. Thus a moderately distributed unstaked attacker (or one cycling many source IPs within limiter constraints) can keep the shared 25% headroom pool continuously saturated with connections destined for QoS rejection.

### Impact Explanation
This is a QoS-evasion / staked-peer-starvation issue: legitimate staked handshakes are refused (`refused_connections_too_many_open_connections`) due to attacker-controlled unstaked connections occupying `ClientConnectionTracker` slots during their handshake-to-rejection window, even though those attacker connections are never actually admitted into the staked or unstaked `ConnectionTable`. This defeats the purpose of the "25% headroom" buffer that was specifically designed to absorb legitimate handshake churn, and the impact is scoped to increased handshake rejection rate for staked/legitimate peers under attacker-driven connection floods — a QoS admission-control weakness rather than a memory-safety or consensus bug.

### Likelihood Explanation
Feasibility depends on how much distinct-IP diversity and connection-attempt rate an attacker can sustain against `overall_connection_rate_limiter` and per-IP `ConnectionRateLimiter`, which I could not fully evaluate: I was unable to confirm the concrete default/production values (bucket sizes, refill rates, `max_staked_connections`/`max_unstaked_connections` defaults) within the available context, since the relevant config-default grep results were not returned before the tool budget ran out. Without those concrete numbers, I cannot conclusively determine whether the rate limiters are sized tightly enough to make this attack impractical in a real deployment, or whether the 25% headroom is large enough in absolute terms that a realistic attacker (bounded by per-IP and global rate limits) could meaningfully deny it to staked peers. This is a plausible design gap based on the code path traced, but confirming exploitability at production-scale requires the missing numeric configuration values and a live load test.

### Recommendation
Not proposing a fix per audit-question scope; flagging that (1) `ClientConnectionTracker` admission should ideally reserve/prioritize headroom for staked or otherwise-prioritized connection attempts rather than using one shared unstructured pool, and (2) the tracker slot should be released as early as possible on rejection paths (already the case via `Drop`, but the window is bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` plus mutex-lock contention in `try_add_connection`, which could be tightened for unstaked-classified connections specifically).

### Proof of Concept
Given the missing concrete rate-limiter/default-capacity values, a full PoC could not be finalized with confidence. A representative integration-test sketch (values TBD based on actual `SwQosConfig`/`ConnectionRateLimiter` defaults):
```rust
// Pseudocode outline - exact constants need confirming from SwQosConfig defaults
// 1. Spawn server with SwQos, small max_unstaked_connections and max_staked_connections.
// 2. Spawn N unstaked clients (N > max_unstaked_connections) from distinct source ports/IPs,
//    each completing the QUIC handshake but exceeding max_connections_per_unstaked_peer
//    or max_unstaked_connections so try_add_connection rejects them.
// 3. Concurrently, attempt a staked client connection.
// 4. Assert stats.refused_connections_too_many_open_connections increments for the staked
//    attempt while attacker handshakes are in-flight, and assert it succeeds once attacker
//    trackers are dropped.
```
Given the uncertainty flagged above, I recommend a live load test against a running validator (or `streamer` test harness) using the confirmed default `SwQosConfig` values to measure `stats.refused_connections_too_many_open_connections` increments under sustained attacker-driven handshake floods, as originally proposed in the audit question's proof idea.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L229-234)
```rust
impl Drop for ClientConnectionTracker {
    /// When this is dropped, reduce the open connection count.
    fn drop(&mut self) {
        self.stats.open_connections.fetch_sub(1, Ordering::Relaxed);
    }
}
```

**File:** streamer/src/nonblocking/quic.rs (L236-251)
```rust
impl ClientConnectionTracker {
    /// Check the max_concurrent_connections limit and if it is within the limit
    /// create ClientConnectionTracker and increment open connection count. Otherwise returns Err
    fn new(stats: Arc<StreamerStats>, max_concurrent_connections: usize) -> Result<Self, ()> {
        let open_connections = stats.open_connections.fetch_add(1, Ordering::Relaxed);
        if open_connections >= max_concurrent_connections {
            stats.open_connections.fetch_sub(1, Ordering::Relaxed);
            debug!(
                "There are too many concurrent connections opened already: open: \
                 {open_connections}, max: {max_concurrent_connections}"
            );
            return Err(());
        }

        Ok(Self { stats })
    }
```

**File:** streamer/src/nonblocking/quic.rs (L346-369)
```rust
            // check overall connection request rate limiter
            if overall_connection_rate_limiter.current_tokens() == 0 {
                stats
                    .connection_rate_limited_across_all
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to overall rate limit.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
            // then perform per IpAddr rate limiting
            if !rate_limiter.is_allowed(&incoming.remote_address().ip()) {
                stats
                    .connection_rate_limited_per_ipaddr
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to per-IP rate limiting.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
```

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

**File:** streamer/src/nonblocking/quic.rs (L456-532)
```rust
#[allow(clippy::too_many_arguments)]
async fn setup_connection<Q, C>(
    connecting: Connecting,
    rate_limiter: Arc<ConnectionRateLimiter>,
    overall_connection_rate_limiter: Arc<TokenBucket>,
    client_connection_tracker: ClientConnectionTracker,
    packet_sender: Sender<PacketBatch>,
    stats: Arc<StreamerStats>,
    server_params: Arc<QuicStreamerConfig>,
    qos: Arc<Q>,
    tasks: TaskTracker,
) where
    Q: QosController<C> + Send + Sync + 'static,
    C: ConnectionContext + Send + Sync + 'static,
{
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
        match connecting_result {
            Ok(new_connection) => {
                debug!("Got a connection {from:?}");
                // now that we have observed the handshake we can be certain
                // that the initiator owns an IP address, we can update rate
                // limiters on the server
                if !rate_limiter.register_connection(&from.ip()) {
                    debug!("Reject connection from {from:?} -- rate limiting exceeded");
                    stats
                        .connection_rate_limited_per_ipaddr
                        .fetch_add(1, Ordering::Relaxed);
                    new_connection.close(
                        CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                        CONNECTION_CLOSE_REASON_DISALLOWED,
                    );
                    return;
                }

                if overall_connection_rate_limiter.consume_tokens(1).is_err() {
                    debug!(
                        "Reject connection from {:?} -- total rate limiting exceeded",
                        from.ip()
                    );
                    stats
                        .connection_rate_limited_across_all
                        .fetch_add(1, Ordering::Relaxed);
                    new_connection.close(
                        CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                        CONNECTION_CLOSE_REASON_DISALLOWED,
                    );
                    return;
                }

                stats.total_new_connections.fetch_add(1, Ordering::Relaxed);

                let mut conn_context = qos.build_connection_context(&new_connection);
                if let Some(cancel_connection) = qos
                    .try_add_connection(
                        client_connection_tracker,
                        &new_connection,
                        &mut conn_context,
                    )
                    .await
                {
                    tasks.spawn(handle_connection(
                        packet_sender.clone(),
                        from,
                        new_connection,
                        stats,
                        server_params.wait_for_chunk_timeout,
                        server_params.max_stream_data_bytes,
                        conn_context.clone(),
                        qos,
                        cancel_connection,
                    ));
                }
```

**File:** streamer/src/nonblocking/quic.rs (L1042-1050)
```rust
        } else {
            if let Some(connection) = connection {
                connection.close(
                    CONNECTION_CLOSE_CODE_TOO_MANY.into(),
                    CONNECTION_CLOSE_REASON_TOO_MANY,
                );
            }
            None
        }
```

**File:** streamer/src/nonblocking/swqos.rs (L415-438)
```rust
                ConnectionPeerType::Unstaked => {
                    if let Ok((last_update, cancel_connection, stream_counter)) = self
                        .prune_unstaked_connections_and_add_new_connection(
                            client_connection_tracker,
                            connection,
                            self.unstaked_connection_table.clone(),
                            self.config.max_unstaked_connections,
                            conn_context,
                        )
                        .await
                    {
                        self.stats
                            .connection_added_from_unstaked_peer
                            .fetch_add(1, Ordering::Relaxed);
                        conn_context.in_staked_table = false;
                        conn_context.last_update = last_update;
                        conn_context.stream_counter = Some(stream_counter);
                        return Some(cancel_connection);
                    } else {
                        self.stats
                            .connection_add_failed_unstaked_node
                            .fetch_add(1, Ordering::Relaxed);
                    }
                }
```

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

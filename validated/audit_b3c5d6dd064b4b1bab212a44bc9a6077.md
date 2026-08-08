### Title
Global connection burst limiter can be bypassed by delaying handshake completion, allowing handshake CPU amplification beyond `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST` - (File: streamer/src/nonblocking/quic.rs)

### Summary
The `run_server` accept loop only *peeks* at `overall_connection_rate_limiter.current_tokens()` before spawning a `setup_connection` task, but the bucket is not actually decremented (`consume_tokens(1)`) until *after* the QUIC handshake completes inside that spawned task. Because token consumption is deferred by up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s), an attacker who keeps handshakes open (but not timing out) can get many more connections admitted into full QUIC handshake processing than the configured global budget (`MAX_CONNECTION_BURST` = 1000, `TOTAL_CONNECTIONS_PER_SECOND` = 2500) allows, up to the much larger `qos.max_concurrent_connections()` ceiling.

### Finding Description
In `streamer/src/nonblocking/quic.rs`, `run_server`'s single sequential accept loop performs a cheap early check: [1](#0-0) 

This only reads `current_tokens()`, it does not reserve/decrement anything. The actual admission decision — `overall_connection_rate_limiter.consume_tokens(1)` — only happens inside `setup_connection`, after the (async, network-latency-bound) handshake `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` resolves: [2](#0-1) 

Because `run_server` spawns `setup_connection` as a detached task (`tasks.spawn(...)`) and immediately loops back to accept the next `Incoming`, the accept loop can admit many connections into the handshake pipeline while none of their `consume_tokens` calls have fired yet — the bucket still shows tokens available. `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` is 2 seconds, so an attacker who paces handshake completion just under that timeout keeps the bucket "full" from the loop's point of view for up to 2 seconds while flooding new `Incoming` connections.

The only other gate preventing unbounded admission during this window is `ClientConnectionTracker::new`, bounded by `qos.max_concurrent_connections()`: [3](#0-2) 

which is derived from staked/unstaked connection limits (defaults sum to on the order of 4000), well above `MAX_CONNECTION_BURST` (1000): [4](#0-3) 

So, within a single ~2-second handshake window, the number of concurrently in-flight QUIC handshakes is effectively bounded by `max_concurrent_connections`, not by `MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND` as the code comment ("protect against connection attempt bursts with a global rate-limiter") implies. This is a genuine gap between the intended global admission budget and what the loop's peek-based check actually enforces, because reservation and consumption are decoupled by an attacker-influenced delay (handshake completion time).

### Impact Explanation
This is QoS/rate-limit evasion of the global connection-burst safety valve documented in the code (`run_server`'s comment block), which exists specifically to "protect against connection attempt bursts" and bound handshake-processing CPU cost cluster-wide before per-IP/staked checks apply. By stalling handshake completion near the 2-second timeout while repeatedly opening new connections from many source addresses/ports, an unprivileged attacker can force the leader to perform full QUIC/TLS handshake cryptographic work for up to `max_concurrent_connections` simultaneous peers per handshake-timeout window, instead of the intended `MAX_CONNECTION_BURST` (1000) cap, amplifying CPU consumption in the QUIC ingress path relative to the operator-configured/hardcoded global budget. This does not by itself cause unbounded memory or a panic (it remains capped by `max_concurrent_connections`), but it does violate the invariant that the global connection-rate limiter bounds handshake admission, and increases CPU pressure on the ingress path that feeds sigverify/scheduling.

### Likelihood Explanation
Exploitation requires only unprivileged network access to the TPU QUIC port and the ability to open many QUIC connections with controlled pacing of the handshake (feasible with standard QUIC client libraries, no valid stake or gossip participation needed). The attack is repeatable indefinitely and does not require winning any true data race — it exploits the deterministic ordering where `current_tokens()` peeks are cheap and `consume_tokens()` is deferred until post-handshake, a design property rather than a rare timing coincidence.

### Recommendation
Reserve a token from `overall_connection_rate_limiter` at accept time (in `run_server`, via `consume_tokens(1)` instead of the peek-only `current_tokens() == 0` check), and release/refund it if the handshake ultimately fails or is rejected for other reasons, so the global budget reflects in-flight handshake attempts, not only completed ones. Alternatively, perform the `consume_tokens` check immediately after `incoming.accept()` returns (before spawning the long-lived handshake future) rather than after the handshake `timeout(...).await` completes.

### Proof of Concept
Integration/unit test plan (in `streamer/src/nonblocking/quic.rs` test module, using existing test harness such as `setup_quic_server`):
1. Configure a test server with a small `MAX_CONNECTION_BURST`-equivalent (or instrument the real constants for test) and a generous `qos.max_concurrent_connections()`.
2. Spawn `N > MAX_CONNECTION_BURST` (e.g., `4 * MAX_CONNECTION_BURST`) concurrent QUIC client connections against the test server, each client intentionally delaying full handshake completion (e.g., pausing after sending Initial packets) for close to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.
3. Instrument `StreamerStats` (`total_new_connections`, `outstanding_incoming_connection_attempts`) or add a test hook counting how many connections reach `setup_connection`'s handshake stage (i.e., pass `ClientConnectionTracker::new` and start `connecting.await`).
4. Assert that the number of connections concurrently in the handshake stage exceeds `MAX_CONNECTION_BURST`, demonstrating the global burst budget was bypassed, bounded only by `max_concurrent_connections`. [5](#0-4) [6](#0-5)

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-80)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;

/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
```

**File:** streamer/src/nonblocking/quic.rs (L236-252)
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
}
```

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L346-357)
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
```

**File:** streamer/src/nonblocking/quic.rs (L371-399)
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

            stats
                .outstanding_incoming_connection_attempts
                .fetch_add(1, Ordering::Relaxed);
            let connecting = incoming.accept();
            match connecting {
                Ok(connecting) => {
                    let rate_limiter = rate_limiter.clone();
                    let overall_connection_rate_limiter = overall_connection_rate_limiter.clone();
                    tasks.spawn(setup_connection(
                        connecting,
                        rate_limiter,
                        overall_connection_rate_limiter,
                        client_connection_tracker,
                        packet_batch_sender.clone(),
                        stats.clone(),
                        quic_server_params.clone(),
                        qos.clone(),
                        tasks.clone(),
                    ));
```

**File:** streamer/src/nonblocking/quic.rs (L471-508)
```rust
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
```

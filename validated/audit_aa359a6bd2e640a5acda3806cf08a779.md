### Title
Per-IP QUIC connection rate limiter can be bypassed via pre-handshake connection churn - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The per-IP `ConnectionRateLimiter` only debits tokens in `register_connection`, which is called from `setup_connection` *after* the QUIC handshake completes. An attacker who aborts the handshake before it finishes is checked only by the non-mutating `is_allowed` (which never consumes tokens), so the per-IP budget is never actually spent, letting a single unstaked IP repeatedly consume shared accept-loop and concurrent-connection capacity.

### Finding Description
In the accept loop `run_server` in `streamer/src/nonblocking/quic.rs`, incoming connections are checked with `rate_limiter.is_allowed(&incoming.remote_address().ip())` [1](#0-0) , which only reads `current_tokens` without mutating state [2](#0-1) . The actual token consumption happens only in `register_connection`, invoked from `setup_connection` after `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` resolves `Ok(new_connection)` [3](#0-2) . If the attacker aborts the handshake (or lets it fail/time out), `register_connection` is never called, so `is_allowed` continues to report the IP as having full tokens indefinitely — the per-IP `KeyedRateLimiter` budget is never actually spent.

Before this per-IP check consumes anything, each accepted `incoming` still passes `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())`, which increments the process-wide `stats.open_connections` counter against a single global `max_concurrent_connections` limit (not per-IP) [4](#0-3) [5](#0-4) , and increments `stats.outstanding_incoming_connection_attempts`, held for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) [6](#0-5) [7](#0-6) . Both of these are shared, non-per-IP pools. A single attacker IP can therefore repeatedly open and abandon handshakes, cycling through the global `open_connections`/`outstanding_incoming_connection_attempts` capacity and consuming the shared `overall_connection_rate_limiter` global token budget (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, burst `1000`) [8](#0-7) [9](#0-8) , without ever tripping `connection_rate_limited_per_ipaddr`. The per-IP mechanism, whose stated purpose is to "rate-limit abusive peers by (control-asserted) ip" [10](#0-9) , is thus evadable by never completing a handshake.

### Impact Explanation
Because `open_connections`/`outstanding_incoming_connection_attempts` and the global `overall_connection_rate_limiter` are shared across all source IPs without per-source fairness beyond the (bypassable) per-IP limiter, one unstaked attacker can monopolize a disproportionate share of the TPU QUIC accept loop's in-flight connection budget and the global connection-attempt rate budget, starving legitimate unstaked/staked peers from establishing connections during the churn window. This falls into the QoS bypass/DoS-via-resource-starvation category rather than a crash or consensus-affecting bug.

### Likelihood Explanation
No stake is required and only network access to the leader's public TPU QUIC port is needed. The attacker only needs to open raw QUIC initial packets and abandon them before the handshake resolves (well within the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`), which is trivial to script and fully repeatable at will, bounded only by the shared global rate limiter (2500/sec, burst 1000), which the attacker alone can consume.

### Recommendation
Consume (or provisionally reserve) a per-IP token at accept time in `is_allowed`/before spawning `setup_connection`, rather than deferring consumption to post-handshake in `register_connection`; release/refund the token on handshake failure/timeout if that behavior is desired, so that repeated pre-handshake churn from the same IP is actually throttled by the per-IP `KeyedRateLimiter`.

### Proof of Concept
Integration test plan (extending the existing `test_rate_limiting`-style tests in `tpu-client-next/tests/connection_workers_scheduler_test.rs` or `streamer/src/nonblocking/quic.rs` test module):
1. Start `run_server`/`spawn_server` with a small `max_connections_per_ipaddr_per_min` (e.g., 1) and a generous `max_concurrent_connections`.
2. From a single source IP, repeatedly create raw QUIC client endpoints, initiate a connection, and immediately drop the client-side endpoint before the handshake future resolves (i.e., before `Connecting` awaited in `setup_connection` completes) — repeat this well beyond the configured per-minute limit (e.g., 50 times in a few seconds).
3. Assert that `stats.total_incoming_connection_attempts` grows to ~50 while `stats.connection_rate_limited_per_ipaddr` stays at 0, demonstrating the per-IP limiter never triggers.
4. Then complete one full handshake from the same IP normally and confirm it is accepted (showing tokens were never depleted despite the churn), violating the intended invariant that per-source connection churn is bounded by the per-IP limiter.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-76)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;
```

**File:** streamer/src/nonblocking/quic.rs (L78-80)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L332-340)
```rust
            // our connection/handshake abuse mitigation policy is one of shed
            // fast and bound resource consumption. attempting to be "smarter"
            // before a peer has asserted control over their ip address by
            // completing the retry challenge creates a scenario whereby peers
            // can attack one another via ip spoofing. employ the following
            // * limit duration of in-flight connection attempts with a timeout
            // * protect against connection attempt bursts with a global rate-limiter
            // * rate-limit abusive peers by (control-asserted) ip
            // * cap total connections per-peer/ip
```

**File:** streamer/src/nonblocking/quic.rs (L358-369)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L380-384)
```rust

            stats
                .outstanding_incoming_connection_attempts
                .fetch_add(1, Ordering::Relaxed);
            let connecting = incoming.accept();
```

**File:** streamer/src/nonblocking/quic.rs (L471-493)
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
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L34-40)
```rust
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }
```

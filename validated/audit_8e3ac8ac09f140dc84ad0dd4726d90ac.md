### Title
Pre-handshake connection churn bypasses connection admission rate limiters and exhausts the shared `ClientConnectionTracker` slot budget on the QUIC accept path - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The `overall_connection_rate_limiter` and per-IP `ConnectionRateLimiter` are only *peeked* (not consumed) before a QUIC handshake completes, and are only actually decremented in `setup_connection` after a successful handshake via `rate_limiter.register_connection` / `overall_connection_rate_limiter.consume_tokens(1)`. An unstaked remote attacker can therefore open (and immediately abandon or close) QUIC connections to `tpu-vote` at essentially unlimited rate without ever registering against these limiters, while still consuming a slot from the shared, un-segregated `ClientConnectionTracker`/`max_concurrent_connections` budget for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) per attempt.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs`), for each `incoming` connection the server:
1. Checks `overall_connection_rate_limiter.current_tokens() == 0` — a non-mutating peek [1](#0-0) .
2. Checks `rate_limiter.is_allowed(&ip)` — also a non-mutating peek that returns `true` for any IP with no prior recorded entry, per `ConnectionRateLimiter::is_allowed` [2](#0-1) [3](#0-2) .
3. Only *after* passing both peeks does it create a `ClientConnectionTracker`, which increments the shared `stats.open_connections` counter and enforces `qos.max_concurrent_connections()` — this cap is not segregated by stake or by source IP [4](#0-3) [5](#0-4) .
4. `setup_connection` then awaits the handshake under a 2-second `timeout` [6](#0-5) . The per-IP token (`rate_limiter.register_connection`) and the global token (`overall_connection_rate_limiter.consume_tokens(1)`) are only actually consumed **after** the handshake completes successfully [7](#0-6) .

Because both admission limiters are pure peeks prior to handshake completion, and neither is decremented unless a peer actually finishes the TLS handshake, an attacker who never completes (or deliberately aborts) the handshake never pays into either limiter. The comment at lines 332-340 explicitly acknowledges the design intent ("protect against connection attempt bursts with a global rate-limiter … rate-limit abusive peers by (control-asserted) ip") but the implementation only enforces this against *completed* handshakes, not against the *attempt* rate itself. The only actual backpressure on attempt rate is the `ClientConnectionTracker`/`max_concurrent_connections` slot cap, which is global and shared across staked/unstaked/legitimate traffic — so a churn attacker holds slots (each up to 2s) at a rate limited only by their own network throughput and the QUIC Initial-packet decrypt cost, forcing the validator to spend CPU on QUIC Initial/handshake crypto processing for every churned attempt while never registering a real connection or sending a fee-bearing packet.

### Impact Explanation
This matches an "unbounded/uncapped resource consumption disproportionate to attacker cost" category: the tpu-vote QUIC endpoint's connection-accept and handshake CPU work, and its `max_concurrent_connections` slot pool (shared by staked/unstaked/legitimate connections), can be monopolized by a zero-stake, zero-fee attacker via rapid half-open churn, since neither the per-IP nor the global connection-rate limiters gate attempt rate — only completed-handshake rate. Sustained churn can starve legitimate voting-related connections from acquiring a `ClientConnectionTracker` slot at all, which is a real availability impact on the vote-ingestion path.

### Likelihood Explanation
Preconditions are minimal: no stake, no prior state, no privileged position — just raw UDP/QUIC connectivity to the public tpu-vote port. The attack is fully repeatable and scriptable (spawn UDP sockets sending a QUIC Initial packet, then optionally abort), requiring only that the attacker never legitimately complete a TLS handshake, which is trivially controllable by the attacker's own client behavior.

### Recommendation
Consume (or at least provisionally reserve) a token from `overall_connection_rate_limiter` and the per-IP `ConnectionRateLimiter` at admission time (before spawning `setup_connection`), refunding it on handshake failure/timeout if desired, rather than only consuming on handshake success. Additionally, consider bounding the number of concurrently in-flight (pre-handshake) `ClientConnectionTracker` slots per source IP, separate from the post-handshake staked/unstaked connection tables, so a single IP cannot monopolize the shared admission budget through churn.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test harness or `connection_rate_limiter.rs`):
```rust
#[tokio::test]
async fn test_prehandshake_churn_bypasses_rate_limiters() {
    // Spin up spawn_server with a small max_concurrent_connections and a
    // small overall_connection_rate_limiter budget (as configured in run_server).
    // From N different attacker sockets (or a single IP), repeatedly:
    //   1. send a raw QUIC Initial packet to the server's tpu-vote UDP endpoint
    //   2. do NOT complete the TLS handshake (drop the socket immediately, or
    //      send a forged Initial-level CONNECTION_CLOSE)
    //   3. repeat as fast as possible for > 2x QUIC_CONNECTION_HANDSHAKE_TIMEOUT
    //
    // Assert via StreamerStats:
    //   - stats.total_incoming_connection_attempts grows far beyond
    //     TOTAL_CONNECTIONS_PER_SECOND * elapsed_seconds (i.e., overall_connection_rate_limiter
    //     did not bound attempt rate)
    //   - stats.connection_rate_limited_across_all and
    //     stats.connection_rate_limited_per_ipaddr remain 0 (limiters never triggered)
    //   - stats.open_connections repeatedly saturates max_concurrent_connections
    //     during the churn window, and a concurrently-attempted legitimate
    //     handshake from a different, well-behaved client is refused
    //     (refused_connections_too_many_open_connections increments) despite
    //     never exceeding TOTAL_CONNECTIONS_PER_SECOND legitimate attempts.
}
```
Expected (buggy) result: attempt counters grow unbounded and the legitimate client is starved out of `max_concurrent_connections`, while `connection_rate_limited_*` stats stay at 0, confirming the admission limiters never engage against pre-handshake churn.

### Citations

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

**File:** streamer/src/nonblocking/quic.rs (L471-476)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
```

**File:** streamer/src/nonblocking/quic.rs (L478-508)
```rust
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

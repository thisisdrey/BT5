### Title
Unstaked attacker can exhaust the global `max_concurrent_connections` slot pool with never-completing QUIC handshakes, ahead of the per-IP rate limiter - ([File: streamer/src/nonblocking/quic.rs])

### Summary
In `run_server`, `ClientConnectionTracker::new` reserves a slot against the global `max_concurrent_connections` limit at accept time, before the QUIC handshake completes, while `ConnectionRateLimiter::register_connection` (the per-IP charge) is only invoked inside `setup_connection` after a successful handshake. An attacker who keeps connections in a not-yet-completed handshake state for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` therefore occupies tracker capacity without ever being charged by the per-IP limiter.

### Finding Description
In `run_server` the sequence is: peek `overall_connection_rate_limiter.current_tokens()` (line 347, no consumption), peek `rate_limiter.is_allowed(&ip)` (line 359, no consumption), then immediately call `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` (lines 371-379), which increments `stats.open_connections` and only fails/refuses (`refused_connections_too_many_open_connections`) once the global cap is hit [1](#0-0) . Only after that, `setup_connection` is spawned, which calls `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` and defers the actual per-IP charge (`rate_limiter.register_connection(&from.ip())`) and the overall bucket consumption (`overall_connection_rate_limiter.consume_tokens(1)`) to *after* the handshake succeeds [2](#0-1) .

Both `is_allowed` and `register_connection` are documented as deliberately deferred until "the initiator owns an IP address" (i.e., after handshake) — `ConnectionRateLimiter::is_allowed` explicitly states it only tracks IPs "with actual confirmed connections" [3](#0-2) , and `register_connection` is the only method that consumes tokens [4](#0-3) . This anti-spoofing design is intentional per the comment block in `run_server` ("attempting to be 'smarter' before a peer has asserted control over their ip address ... creates a scenario whereby peers can attack one another via ip spoofing") [5](#0-4) .

However, this means the `ClientConnectionTracker` (global `max_concurrent_connections`/`open_connections` slot) is the *only* gate applied before handshake completion, and it is a purely global counter — not keyed by IP. A client that legitimately owns its IP (no spoofing needed, since this is a real TCP/UDP-reachable unstaked attacker per the threat model) can open many QUIC connection attempts and deliberately stall/slow-play the handshake (e.g., partial Initial packets, no reply to server flight, or artificially delayed ACKs) so that the connection sits "in-flight" for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` without ever calling into `setup_connection`'s post-handshake success branch. Each such attempt already consumed one `ClientConnectionTracker` slot at accept time and is never charged against `rate_limiter` (since `register_connection` is never reached), so the per-IP `ConnectionRateLimiter` never triggers for these connections, and repeated attempts from the same IP keep succeeding through `is_allowed` (since no tokens were ever consumed). By cycling many overlapping half-open connections (bounded only by local ephemeral ports / OS socket limits), the attacker can keep `stats.open_connections` pinned near `max_concurrent_connections`, causing `ClientConnectionTracker::new` to return `Err(())` for legitimate senders and increment `refused_connections_too_many_open_connections` (lines 241-248, 371-379) [6](#0-5) .

### Impact Explanation
This is a QoS-evasion / connection-limit-bypass causing starvation of legitimate unstaked TPU senders: `max_concurrent_connections` is the shared global cap enforced by `ClientConnectionTracker`, and it can be monopolized by an attacker whose connections are never actually admitted as "registered" per-IP connections, defeating the invariant that the per-IP rate limiter bounds each IP's contribution to concurrent connection state. This matches the described bounty category of QoS/connection-limit evasion leading to TPU capacity starvation for other unstaked senders.

### Likelihood Explanation
Feasible with only unprivileged network access to the leader's TPU QUIC port: the attacker does not need a valid stake, gossip presence, or any spoofing — only the ability to open TCP/UDP sockets and stall the QUIC handshake (e.g. by controlling a custom QUIC client that withholds handshake completion, or by opening the connection and pausing I/O). The window is bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, but as long as the attacker recycles connections faster than they time out and opens enough of them concurrently, the global slot pool can be kept saturated indefinitely. The overall `overall_connection_rate_limiter` check at accept time only peeks `current_tokens() == 0` without consuming, so it does not prevent a burst of non-completing accepts either [7](#0-6) .

### Recommendation
Reserve/charge the per-IP and/or overall rate limiter at accept time (before granting a `ClientConnectionTracker` slot) using a lightweight, spoof-resistant scheme (e.g., a coarse per-source-address token consumed optimistically and refunded/adjusted after handshake proof), or bound the number of concurrently-outstanding (pre-handshake) `ClientConnectionTracker` slots per source IP independently of the global cap, so a single IP cannot consume an unbounded share of `max_concurrent_connections` while its connections remain unauthenticated.

### Proof of Concept
Integration test plan (extends existing `streamer::nonblocking::quic` test harness, e.g. near `test_client_connection_tracker` and `setup_quic_server`):
1. Configure `QuicStreamerConfig` with a small `max_concurrent_connections` (e.g., 5) via `qos.max_concurrent_connections()`.
2. Spawn the server via `setup_quic_server` as in existing tests.
3. From a set of distinct source ports/sockets (simulating distinct unstaked clients), open QUIC connections but intentionally stall completion of the handshake (e.g., use a raw UDP socket / partially-implemented QUIC client that sends the Initial packet then stops responding) for a duration close to but under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.
4. Assert `stats.open_connections` reaches `max_concurrent_connections` while `stats.total_new_connections` and `rate_limiter` registrations remain at 0.
5. Attempt a legitimate connection (full valid handshake) and assert it is refused, with `stats.refused_connections_too_many_open_connections` incremented, demonstrating starvation of a legitimate sender purely from unregistered, non-handshake-completing connections.

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

**File:** streamer/src/nonblocking/quic.rs (L331-340)
```rust
        if let Ok(Some(incoming)) = timeout_connection {
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

**File:** streamer/src/nonblocking/quic.rs (L358-379)
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

**File:** streamer/src/nonblocking/quic.rs (L471-509)
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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L31-40)
```rust
    /// Check if the connection from the said `ip` is allowed.
    /// Here we assume that only IPs with actual confirmed connections are stored in it,
    /// since we should only modify server state once source IP is verified
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L42-50)
```rust
    pub fn register_connection(&self, ip: &IpAddr) -> bool {
        if self.limiter.consume_tokens(*ip, 1).is_ok() {
            debug!("Request from IP {ip:?} allowed");
            true // Request allowed
        } else {
            debug!("Request from IP {ip:?} blocked");
            false // Request blocked
        }
    }
```

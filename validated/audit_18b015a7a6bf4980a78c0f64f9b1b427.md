## Analysis

The question's premise doesn't match the code's actual mitigation design, but the underlying finding it points at is real and confirmed by the code.

Contrary to the question's framing ("checked post-handshake ... letting the attacker complete many cheap 0-RTT-rejected handshakes"), the `ClientConnectionTracker` slot (which enforces `max_concurrent_connections()`) is actually claimed **before** the handshake even begins, in `run_server`, at the point `incoming.accept()` is called [1](#0-0) . The only gates preceding this slot allocation are (1) a non-consuming check `overall_connection_rate_limiter.current_tokens() == 0` and (2) `rate_limiter.is_allowed(ip)` [2](#0-1) .

Critically, `ConnectionRateLimiter::is_allowed` for an IP with no prior record always returns `true` ("if we have not seen IP, allow connection request"), and the global `overall_connection_rate_limiter` token bucket is only decremented in `setup_connection` *after* a successful handshake via `overall_connection_rate_limiter.consume_tokens(1)` [3](#0-2) [4](#0-3) . Per-IP registration via `register_connection` similarly only fires post-handshake [5](#0-4) .

This means: pre-handshake, an attacker's connection attempts are essentially ungated (first contact with a fresh IP always passes `is_allowed`, and the global bucket is only checked, not consumed), yet each accepted `Incoming` immediately consumes a global `ClientConnectionTracker` slot bound by `qos.max_concurrent_connections()` (e.g. `(max_staked_connections + max_unstaked_connections) * 5 / 4` in `swqos.rs`) [6](#0-5) . The slot is held for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) via `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` in `setup_connection` [7](#0-6) [8](#0-7) , and is only released when the `ClientConnectionTracker` is dropped (either after handshake success/failure via connection handling, or via timeout) [9](#0-8) .

So an attacker who opens more than `max_concurrent_connections()` connection attempts within the 2-second handshake window (never needing to actually complete a handshake — abandoning after the Initial packet is enough to occupy a slot for the full timeout) can starve the global open-connection counter, causing `refused_connections_too_many_open_connections` for all other clients (both unstaked and staked), since `open_connections` is a single shared atomic counter, not partitioned per IP or per stake class at the `ClientConnectionTracker` level. This is a legitimate resource-exhaustion path, though it is not "post-handshake" evasion as characterized in the question — it's a pre-handshake gap in the rate limiters, whose per-IP/global consumption logic is deliberately deferred until after handshake to avoid punishing spoofed source IPs (per the code comment at lines 332–340), but this design leaves the shared connection-slot pool exposed to cheap, incomplete-handshake churn.

I could not fully verify at what layer (e.g., XDP retry-token / stateless retry) QUIC's built-in anti-amplification/retry mechanism might already require proof of address ownership before `Endpoint::accept()` yields an `Incoming` in this codebase's `quinn` configuration, which would materially affect the real-world exploitability of "abandoning after Initial packet." This detail (whether `configure_server` enables `retry` requiring a round trip before `incoming.accept()` fires) would need confirmation from `configure_server` in `streamer/src/quic.rs`, which I did not get to inspect due to iteration limits.

### Title
Pre-handshake connection-slot exhaustion via `ClientConnectionTracker` bypassing per-IP/global rate limiter consumption - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ClientConnectionTracker::new` (bounding `max_concurrent_connections()`) is consumed in `run_server` before the QUIC handshake completes, gated only by non-consuming/first-seen-always-allowed checks (`overall_connection_rate_limiter.current_tokens()==0` and `ConnectionRateLimiter::is_allowed`). Real rate-limiter token consumption (`register_connection`, `overall_connection_rate_limiter.consume_tokens`) happens only after handshake success in `setup_connection`, so an attacker can occupy the shared connection-slot pool for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` per attempt without ever being rate-limited or completing a handshake.

### Finding Description
`run_server` accepts `Incoming` connections and performs two cheap checks before allocating a `ClientConnectionTracker` slot: `overall_connection_rate_limiter.current_tokens() == 0` (a read-only check, not a consumption) and `rate_limiter.is_allowed(ip)`, which returns `true` for any IP with no existing record [2](#0-1) [3](#0-2) . Passing these, `ClientConnectionTracker::new` immediately increments `stats.open_connections` and is only rejected once `open_connections >= max_concurrent_connections()` [10](#0-9) . The tracker is then handed to `setup_connection`, which bounds the handshake with `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` [8](#0-7) ; the actual per-IP (`register_connection`) and global (`consume_tokens`) rate-limit consumption occurs only after a successful handshake [4](#0-3) . Because slot allocation precedes rate-limiter consumption and handshake completion, an attacker can open many connection attempts from one or more fresh IPs, abandon them before handshake completes, and hold slots for the full timeout window, unrestrained by either rate limiter since neither has been "charged" yet.

### Impact Explanation
This exhausts the shared, non-partitioned `open_connections` counter used for `max_concurrent_connections()` across all peer types, causing `refused_connections_too_many_open_connections` for legitimate staked and unstaked clients alike, denying TPU ingress — a remote DoS on connection admission via connection-slot exhaustion, matching an ingress/QoS-evasion DoS bounty category.

### Likelihood Explanation
Requires only an unstaked remote attacker able to open UDP/QUIC connection attempts to the public TPU port and abandon them before handshake completes; no stake, keys, or special config needed. Repeatable continuously by cycling connection attempts faster than the 2-second handshake timeout, from one or a few source IPs (since per-IP `is_allowed` doesn't block first contact and doesn't gate slot allocation before consumption).

### Recommendation
Consume/charge the per-IP and global connection-rate-limiter tokens (or an equivalent lightweight pre-handshake budget) at slot-allocation time in `run_server`, not only after handshake success in `setup_connection`, or introduce a separate, tightly bounded pre-handshake "in-flight/pending" slot pool distinct from the fully-established `max_concurrent_connections()` pool so a burst of abandoned handshakes cannot starve legitimate established-connection capacity.

### Proof of Concept
Integration test in `streamer/src/nonblocking/quic.rs` test module:
1. Configure a `SwQos`/`spawn_server` instance with a small `max_concurrent_connections()` (e.g., via small `max_staked_connections`/`max_unstaked_connections`).
2. Spawn `max_concurrent_connections() + K` concurrent client `Endpoint::connect` futures from a single test process (simulating one or more attacker-controlled sockets), but instead of awaiting the full handshake, drop the client endpoint or delay past acceptance without completing the QUIC handshake (e.g., use a client that sends the Initial packet then stalls).
3. Assert `stats.refused_connections_too_many_open_connections` increments for excess attempts (K), and that a legitimate, well-behaved client connection issued in parallel from a distinct source is rejected/starved within `WAIT_FOR_CONNECTION_TIMEOUT`, only recovering after `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` elapses for the abandoned attempts — confirming slot exhaustion occurs before any rate-limiter token is consumed.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L80-80)
```rust
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
```

**File:** streamer/src/nonblocking/quic.rs (L229-234)
```rust
impl Drop for ClientConnectionTracker {
    /// When this is dropped, reduce the open connection count.
    fn drop(&mut self) {
        self.stats.open_connections.fetch_sub(1, Ordering::Relaxed);
    }
}
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

**File:** streamer/src/nonblocking/quic.rs (L371-384)
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
```

**File:** streamer/src/nonblocking/quic.rs (L472-475)
```rust
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/quic.rs (L483-508)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

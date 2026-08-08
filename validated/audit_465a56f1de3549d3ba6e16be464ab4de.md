### Title
Overall/per-IP QUIC connection rate limiters are only consumed on handshake success, allowing incomplete handshakes to exhaust global `open_connections`/`outstanding_incoming_connection_attempts` budget and starve legitimate connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` and `setup_connection` in `streamer/src/nonblocking/quic.rs` only *peek* at `overall_connection_rate_limiter` and the per-IP `ConnectionRateLimiter` before/during a handshake, and only actually *consume* tokens from them after a handshake succeeds. An attacker who opens QUIC connections but never completes the handshake (or deliberately aborts it) never causes these limiters to deplete, so the stated defenses ("protect against connection attempt bursts with a global rate-limiter", "rate-limit abusive peers by ip") provide no protection against connection churn from incomplete handshakes.

### Finding Description
In `run_server`, for each accepted `Incoming`:
- The global limiter is checked with `overall_connection_rate_limiter.current_tokens() == 0` [1](#0-0) , which is a non-mutating read (`TokenBucket::current_tokens` only updates/refills, never subtracts) [2](#0-1) .
- The per-IP limiter is checked with `rate_limiter.is_allowed(&ip)` [3](#0-2) , which also only peeks (`current_tokens(ip) > 0`) without consuming [4](#0-3) .
- Actual token consumption (`overall_connection_rate_limiter.consume_tokens(1)` and `rate_limiter.register_connection(&ip)`) only happens inside `setup_connection` **after** the QUIC handshake has already completed successfully [5](#0-4) .

Consequently, if the peer never completes the handshake within `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) — e.g., sends a malformed Initial, or an Initial and then goes silent — `setup_connection`'s `timeout(...)` branch simply increments `connection_setup_timeout` and returns without ever touching either rate limiter [6](#0-5) [7](#0-6) . Since the buckets are never depleted, `current_tokens()`/`is_allowed()` will keep reporting available tokens indefinitely for such traffic, i.e., the "global rate-limiter" and "per-IP" mitigations described in the code comment above the check [8](#0-7)  are entirely bypassed by non-completing handshakes.

The only mechanism that still gates such connections is `ClientConnectionTracker::new`, which increments a single global `stats.open_connections` counter and rejects once it reaches `qos.max_concurrent_connections()` [9](#0-8) . This counter is shared across staked and unstaked, and across all connection lifecycle stages ("Incoming, Connecting, Connection") [10](#0-9) . `outstanding_incoming_connection_attempts` is tracked but never used as an admission gate — it's purely a stat [11](#0-10) . Thus an attacker that keeps `open_connections` pinned at `max_concurrent_connections` by continuously initiating (never-completing) handshakes at a rate exceeding `1/QUIC_CONNECTION_HANDSHAKE_TIMEOUT * max_concurrent_connections` will cause every subsequent legitimate `Incoming` (staked or unstaked) to be refused via `refused_connections_too_many_open_connections` [12](#0-11) , entirely bypassing both the global and per-IP token-bucket defenses that were specifically designed to prevent this kind of churn.

### Impact Explanation
This is a global TPU connection-accept denial of service: the leader's `open_connections` slot budget (bounded by `qos.max_concurrent_connections()`, sourced from `DEFAULT_MAX_STAKED_CONNECTIONS`/`DEFAULT_MAX_UNSTAKED_CONNECTIONS`, e.g. 2000+2000) can be continuously saturated by an unstaked attacker using incomplete handshakes from many source ports, since neither the overall nor per-IP token buckets are actually depleted by these attempts. Legitimate staked and unstaked peers are then refused new connections cluster-wide for that leader slot, halting transaction ingress via QUIC — this matches the "grossly underpriced pre-fee work / QoS evasion / DoS" bounty category for critical ingress.

### Likelihood Explanation
Fully feasible for an unprivileged, unstaked remote attacker: no stake, no key material, and no completed handshake is required. The attacker only needs to open UDP sockets, send an Initial packet (or malformed one) toward the TPU QUIC port, and never/slowly respond to the handshake, repeating from many source ports/IPs faster than the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window closes. Because per-IP and global token buckets are never depleted by this behavior, there is no rate cap preventing the attacker from opening connection attempts as fast as the network/OS allows, well above `MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND`, which are only enforced against successful connections.

### Recommendation
Consume (or provisionally reserve) tokens from both `overall_connection_rate_limiter` and the per-IP `ConnectionRateLimiter` at admission time (when the `Incoming` is accepted in `run_server`), not after handshake completion in `setup_connection`. If the handshake later fails/times out, either leave the token consumed (fail-closed accounting) or refund it, but the accounting must reflect connection *attempts*, not only successes. Additionally, `outstanding_incoming_connection_attempts` should be used as an explicit admission-control gate (a hard cap on in-flight, not-yet-handshaked connections) independent of `max_concurrent_connections`, so that partial handshakes cannot consume the same budget as fully-open connections indefinitely.

### Proof of Concept
Integration test plan (extends the existing `streamer/src/nonblocking/quic.rs` test module which already has `setup_quic_server`/`make_client_endpoint` helpers):
1. Start a QUIC server via `setup_quic_server` with a small `max_concurrent_connections` (via `SwQosConfig`) to make the PoC deterministic.
2. Spawn N client tasks (N > `max_concurrent_connections`) that each open a UDP socket and send a single malformed QUIC Initial packet to the server's TPU port (or a real quinn client `Connecting` that is dropped immediately without completing handshake), without ever completing the TLS handshake.
3. Immediately (before `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` elapses), attempt a normal, fully-valid client connection with `make_client_endpoint`.
4. Assert:
   - `stats.refused_connections_too_many_open_connections` > 0 for the legitimate connection attempt (i.e., it gets refused), while
   - `stats.connection_rate_limited_across_all` and `stats.connection_rate_limited_per_ipaddr` remain 0 for the flood traffic (proving the rate limiters never engaged), demonstrating the limiter bypass and resulting starvation of the legitimate client's connection slot within the SLA (`QUIC_CONNECTION_HANDSHAKE_TIMEOUT`).

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

**File:** streamer/src/nonblocking/quic.rs (L471-475)
```rust
    let from = connecting.remote_address();
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

**File:** streamer/src/nonblocking/quic.rs (L538-542)
```rust
    } else {
        stats
            .connection_setup_timeout
            .fetch_add(1, Ordering::Relaxed);
    }
```

**File:** net-utils/src/token_bucket.rs (L62-70)
```rust
    /// Return current amount of tokens in the bucket.
    /// This may be somewhat inconsistent across threads
    /// due to Relaxed atomics.
    #[inline]
    pub fn current_tokens(&self) -> u64 {
        let now = self.time_us();
        self.update_state(now);
        self.tokens.load(Ordering::Relaxed)
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

**File:** streamer/src/quic.rs (L225-226)
```rust
    // All connections in various states such as Incoming, Connecting, Connection
    pub(crate) open_connections: AtomicUsize,
```

**File:** streamer/src/quic.rs (L231-233)
```rust
    pub(crate) refused_connections_too_many_open_connections: AtomicUsize,
    pub(crate) outstanding_incoming_connection_attempts: AtomicUsize,
    pub(crate) total_incoming_connection_attempts: AtomicUsize,
```

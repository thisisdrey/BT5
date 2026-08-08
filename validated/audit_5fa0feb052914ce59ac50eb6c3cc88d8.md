### Title
Single-IP QUIC handshake-stalling exhausts the global open-connection budget before `ConnectionRateLimiter` ever applies - ([File: streamer/src/nonblocking/quic.rs])

### Summary
An unstaked attacker can open QUIC connections that are accepted by `run_server` (passing the pre-handshake overall/per-IP checks) but never complete the TLS handshake, holding an `open_connections` slot for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) each. Because `ConnectionRateLimiter::register_connection` is only invoked after a *successful* handshake, and `ConnectionRateLimiter::is_allowed` unconditionally returns `true` for any IP that has never registered a connection, a single attacker IP can repeatedly cycle stalled handshakes to keep the global concurrency budget saturated indefinitely, starving legitimate connection setups.

### Finding Description
In `run_server`, the pre-handshake admission gate for a new `Incoming` is, in order: the global `overall_connection_rate_limiter` token check, then `rate_limiter.is_allowed(&ip)`, then `ClientConnectionTracker::new(..., qos.max_concurrent_connections())`, and only then is `outstanding_incoming_connection_attempts` incremented and `setup_connection` spawned [1](#0-0) .

`ConnectionRateLimiter::is_allowed` treats any IP with no prior record as automatically allowed (`None => true`), and explicitly documents that it assumes "only IPs with actual confirmed connections are stored in it" [2](#0-1) . The only place that records/consumes a token for an IP is `register_connection`, which is called from `setup_connection` **only in the `Ok(new_connection)` branch after the handshake succeeds** [3](#0-2) . If the handshake instead times out, the code path goes to the `else` branch which merely increments `connection_setup_timeout` and never calls `register_connection` [4](#0-3) .

Consequently, an attacker who deliberately never completes the handshake (e.g., sends Initial + partial/garbage Handshake data, or nothing at all) from a single, un-spoofed source IP will pass `is_allowed` on every single connection attempt, because that IP is never "registered." Each such connection consumes an `open_connections` slot the moment `ClientConnectionTracker::new` succeeds [5](#0-4) , and that slot is only released when `ClientConnectionTracker` is dropped — which for a stalled handshake happens only after the full `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) elapses in `setup_connection`'s `timeout(...)` call [6](#0-5)  and `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` const [7](#0-6) .

`ClientConnectionTracker::new` enforces a hard global cap (`qos.max_concurrent_connections()`) on the number of connections in *any* stage — including in-flight handshakes not yet admitted to any per-peer/staked/unstaked table — since `open_connections` is incremented at accept time, before the handshake completes or QoS classification happens [5](#0-4) . If an attacker keeps this counter pinned at the cap by continuously spinning up new stalled handshakes just under 2 seconds apart (from one IP, so `is_allowed` never blocks them), every other incoming connection — including legitimate staked/unstaked peers — gets refused via `ClientConnectionTracker::new` returning `Err`, bumping `refused_connections_too_many_open_connections` and calling `incoming.refuse()` [8](#0-7) . This is exactly the "work spent per packet before any fee/stake is checked stays bounded" invariant being violated: the attacker never pays anything (no stake, no registered rate-limit hit, no fee) yet occupies handshake slots that gate all other traffic.

The comment in `run_server` acknowledges the design tradeoff ("employ...limit duration of in-flight connection attempts with a timeout... rate-limit abusive peers by (control-asserted) ip") [9](#0-8) , but the per-IP rate limiter is a no-op against an attacker that never asserts control (never finishes handshake), so the "duration...with a timeout" bound (2s) is the only throttle, and it can be re-triggered indefinitely by reopening new connections just before expiry.

### Impact Explanation
Scoped impact: handshake-slot exhaustion delaying/blocking legitimate connection setups, matching the described `connection_setup_timeout`/`refused_connections_too_many_open_connections` starvation category. A single unstaked attacker machine holding `max_concurrent_connections` slots occupied via cheap, unauthenticated, un-fee'd stalled handshakes denies the TPU QUIC endpoint to legitimate stake-weighted clients for as long as the attack is sustained.

### Likelihood Explanation
Feasible with only a single unstaked machine/IP and no cryptographic material beyond a self-signed TLS client identity to reach `incoming.accept()`; no stake or fee is required at any stage checked here. The attacker only needs to keep `max_concurrent_connections` connections perpetually "in flight," each held for slightly under 2 seconds, then reopened — a cheap, easily automatable, and fully repeatable loop bounded only by `overall_connection_rate_limiter`'s 2500 conn/s sustained / 1000 burst limits [10](#0-9) , which are far above what's needed to refill a `max_concurrent_connections`-sized pool every 2 seconds.

### Recommendation
Track "attempted" (not just successfully-registered) connections per IP prior to handshake completion, e.g., call a lightweight `is_allowed`/pre-reserve check that also accounts for outstanding unresolved handshake attempts per-IP, or make `ClientConnectionTracker` per-IP-scoped (in addition to the global cap) so a single IP cannot monopolize the global `open_connections` budget with never-completing handshakes. Alternatively, register a provisional rate-limit token at accept time (before the handshake) and only refund it on success, so stalling handshakes still consumes the attacker's own per-IP quota instead of being invisible to `ConnectionRateLimiter`.

### Proof of Concept
Rust integration/fuzz-test plan (using `streamer`'s existing `setup_quic_server`/`make_client_endpoint` test harness):
1. Configure `QuicStreamerConfig` with a realistic `max_concurrent_connections` value.
2. From a single client IP (loopback, single `SocketAddr`), spawn `max_concurrent_connections` QUIC `Connecting` futures that complete the QUIC Initial exchange (reach the server's `incoming.accept()`) but never send valid TLS Finished data (e.g., open the connection but immediately stop driving I/O, or hold the connecting future without awaiting completion).
3. Repeat this cycle, each batch timed to close/reopen just under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s), for several timeout windows.
4. Concurrently, attempt one legitimate `make_client_endpoint` connection and measure time-to-accept.
5. Assert: (a) `stats.outstanding_incoming_connection_attempts` stays pinned near `max_concurrent_connections` throughout the attack window; (b) `stats.refused_connections_too_many_open_connections` increments for the legitimate client's attempt, or its accept latency exceeds an acceptable bound (e.g., > 2s), demonstrating unbounded delay regardless of attacker stall count within the sustained-attack budget.

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

**File:** streamer/src/nonblocking/quic.rs (L346-384)
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

**File:** streamer/src/nonblocking/quic.rs (L470-493)
```rust
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
```

**File:** streamer/src/nonblocking/quic.rs (L538-542)
```rust
    } else {
        stats
            .connection_setup_timeout
            .fetch_add(1, Ordering::Relaxed);
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

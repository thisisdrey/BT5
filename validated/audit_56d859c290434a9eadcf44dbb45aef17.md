### Title
Single unstaked-IP QUIC half-open-handshake flood exhausts `ClientConnectionTracker` capacity and triggers `refused_connections_too_many_open_connections` for legitimate peers - (File: streamer/src/nonblocking/quic.rs)

### Summary
`run_server`/`setup_connection` admit a connection into the `ClientConnectionTracker` capacity pool (`open_connections`) *before* the QUIC handshake completes, while the per-IP and global connection rate limiters are only "charged" (via `register_connection`/`consume_tokens`) *after* a successful handshake. An attacker from a single unstaked IP can therefore repeatedly open connections that stall the handshake for just under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) without ever being throttled by either rate limiter, continuously occupying up to `max_concurrent_connections` tracker slots and starving legitimate staked/unstaked peers.

### Finding Description
In `run_server` [1](#0-0)  the accept loop performs, in order:
1. `overall_connection_rate_limiter.current_tokens() == 0` check - read-only, tokens are only *consumed* later in `setup_connection` after a successful handshake (`overall_connection_rate_limiter.consume_tokens(1)`), so an attacker whose handshakes never complete never depletes this bucket.
2. `rate_limiter.is_allowed(&ip)` - for an IP with no prior *registered* (i.e., handshake-confirmed) connection, `ConnectionRateLimiter::is_allowed` always returns `true` because it has no record for that IP [2](#0-1) ; `register_connection` (which actually consumes a token) is only called after handshake success in `setup_connection` [3](#0-2) .
3. `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` - this is the only gate that increments `open_connections` and enforces the fixed capacity [4](#0-3) . If capacity is exceeded, `refused_connections_too_many_open_connections` is incremented and the connection refused [5](#0-4) .

Once past this gate, the connection (holding the tracker) is handed to `setup_connection`, which awaits `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` [6](#0-5) , with `QUIC_CONNECTION_HANDSHAKE_TIMEOUT = Duration::from_secs(2)` [7](#0-6) . The `ClientConnectionTracker` is only released (via `Drop`, decrementing `open_connections`) when this future resolves - either on handshake success/failure or on the 2s timeout [8](#0-7) .

Because rate-limiter accounting is deliberately deferred until the peer has "asserted control over their ip address" (per the code's own comment) [9](#0-8) , an attacker who never completes the handshake is never subject to per-IP or global connection throttling. The only bound on concurrent unconfirmed connections from one IP is the shared, global `max_concurrent_connections` capacity of `ClientConnectionTracker`, which is deliberately sized only ~25% above the sum of `max_staked_connections`/`max_unstaked_connections` to leave headroom for in-flight *legitimate* handshakes (e.g., `self.config.max_staked_connections * 5 / 4` in `SwQos`) [10](#0-9) .

By opening a stream of connections that stall the handshake for just under 2 seconds and repeating this continuously, a single unstaked attacker can keep up to `max_concurrent_connections` slots perpetually occupied, since new stalled attempts refill the pool as older ones time out. Legitimate connecting peers (staked or unstaked) then hit `ClientConnectionTracker::new`'s capacity check and are refused via `refused_connections_too_many_open_connections`.

### Impact Explanation
This is a QoS-evasion / ingress-capacity-starvation issue on the public TPU QUIC listener: an unprivileged, unstaked attacker can deny legitimate senders (including staked validators) the ability to open QUIC connections to submit transactions, without needing multiple source IPs, stake, or any privileged access - only enough bandwidth to keep several dozen/hundred TCP-like half-open QUIC handshakes alive concurrently. This falls under "QoS evasion" / ingress-buffer starvation as scoped in the prompt.

### Likelihood Explanation
Feasible and repeatable with commodity resources: the attacker needs only to open QUIC connections to the leader's TPU QUIC port and delay/abandon the handshake (e.g., send `Initial` but never send/complete `Handshake`), holding each attempt for just under 2 seconds, and repeat continuously from one IP. No stake, leader control, or gossip/peer control is required, satisfying the "unprivileged attacker" precondition. The number of concurrent slots needed equals `max_concurrent_connections` (a fixed, config-derived constant, e.g., a few hundred to low thousands depending on deployment), which is a modest, sustainable connection count for a single attacking host or small botnet.

### Recommendation
Charge (or partially charge) the per-IP and/or a lightweight per-IP "in-flight attempt" limiter at accept time, before granting a `ClientConnectionTracker` slot, rather than only after handshake completion - e.g., maintain a separate, IP-keyed bound on the number of *outstanding* (not-yet-handshake-confirmed) connection attempts, independent of the global `max_concurrent_connections`, so a single IP cannot monopolize a large fraction of the shared capacity pool. Alternatively, reserve only a small fraction of `max_concurrent_connections` for pre-handshake ("outstanding") attempts per IP and treat post-handshake slots separately.

### Proof of Concept
Integration test plan (extending existing `setup_quic_server` test harness in `streamer/src/nonblocking/testing_utilities.rs`):
1. Start server via `setup_quic_server` with a small `SwQosConfig` (e.g., `max_unstaked_connections = 10`, `max_staked_connections = 0`) so `max_concurrent_connections` is small and deterministic (`10 * 5 / 4 = 12`).
2. From a single client IP (e.g., loopback with distinct ports, simulating one attacker), spawn `N = max_concurrent_connections` raw UDP/QUIC "connections" that send only an `Initial` packet and then stop responding (or use a client `Endpoint` that starts `connect()` but is dropped/stalled before completing the handshake), and repeat this stall-and-reopen loop faster than `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s).
3. Concurrently, from a second legitimate client, attempt `make_client_endpoint(&server_address, None).await` and complete a full handshake plus send a transaction.
4. Assert that during the stall window, `stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0` and that the legitimate client's connection either fails or is significantly delayed, while `stats.connection_rate_limited_per_ipaddr` and `stats.connection_rate_limited_across_all` remain `0` for the attacker's stalled attempts (proving the rate limiters never engaged).

### Citations

**File:** streamer/src/nonblocking/quic.rs (L78-80)
```rust
/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
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

**File:** streamer/src/nonblocking/quic.rs (L342-379)
```rust
            stats
                .total_incoming_connection_attempts
                .fetch_add(1, Ordering::Relaxed);

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

**File:** streamer/src/nonblocking/quic.rs (L483-493)
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

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```

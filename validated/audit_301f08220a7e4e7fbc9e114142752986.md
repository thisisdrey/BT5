### Title
Per-IP and global connection-attempt rate limiters can be fully bypassed by never completing the QUIC handshake, allowing a single unstaked attacker to exhaust the shared `max_concurrent_connections` pool - ([File: streamer/src/nonblocking/quic.rs])

### Summary
In `run_server`, incoming QUIC connections are gated by an `overall_connection_rate_limiter` and a per-IP `ConnectionRateLimiter`, but both are only *checked* (`current_tokens`/`is_allowed`) before the handshake and only *consumed* (`consume_tokens`/`register_connection`) in `setup_connection` after the handshake succeeds. An attacker who never completes the handshake therefore never consumes tokens from either limiter, so `is_allowed` for a never-registered IP always returns `true` and the global bucket is never drained. The only real backpressure against unfinished handshakes is the shared, IP-agnostic `max_concurrent_connections` slot count guarded by `ClientConnectionTracker::new`, bounded in time only by the 60s `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.

### Finding Description
`run_server` (`streamer/src/nonblocking/quic.rs:254-414`) processes each accepted `Incoming` as follows:
1. Checks `overall_connection_rate_limiter.current_tokens() == 0` (read-only, no consumption) at [1](#0-0) .
2. Checks per-IP `rate_limiter.is_allowed(&ip)` (read-only) at [2](#0-1) . `ConnectionRateLimiter::is_allowed` explicitly documents that unseen IPs are always allowed and that the limiter should "only be modified once source IP is verified": [3](#0-2) .
3. Allocates a slot via `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())`, which increments a single global `open_connections` counter shared by *all* source IPs and peer types: [4](#0-3) .
4. Spawns `setup_connection`, which awaits the handshake under `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` (60 seconds): [5](#0-4) .
5. Only **after** a successful handshake does it call `rate_limiter.register_connection(&from.ip())` (which consumes a per-IP token) and `overall_connection_rate_limiter.consume_tokens(1)` (which consumes the global token): [6](#0-5) .

Because both rate limiters are consumed only on handshake success, an attacker who opens many connections and deliberately never completes the handshake (e.g., sends the QUIC Initial packet then stops responding) never triggers either limiter's token consumption. Each such attempt still consumes one slot of the single, IP-agnostic `max_concurrent_connections` budget for up to 60 seconds (`QUIC_CONNECTION_HANDSHAKE_TIMEOUT`). `max_concurrent_connections` is computed as `(max_staked_connections + max_unstaked_connections) * 5/4` for the `SwQos` implementation, or `max_staked_connections * 5/4` for `SimpleQos`: [7](#0-6) [8](#0-7) . This is a fixed global number (e.g. `(2000+2000)*5/4 = 5000` with defaults: [9](#0-8) ), not scoped per source IP.

The code's own design comment acknowledges the tradeoff — pre-handshake per-IP limiting is intentionally avoided to prevent IP-spoofing abuse — and lists mitigations: handshake timeout, global rate limiter, per-IP rate limiting "by (control-asserted) ip", and per-peer connection caps: [10](#0-9) . However, none of the listed mitigations actually bound the *rate* or *count* of concurrently outstanding, never-completed handshakes from a single attacker IP; only the shared `max_concurrent_connections` ceiling and the 60s timeout do, and that ceiling is shared across the whole node, not the attacker's IP.

An unprivileged, unstaked remote attacker connecting to the public TPU/TPU-forward QUIC port can therefore: open connections (optionally rotating ephemeral source ports, though not required since `is_allowed`/`current_tokens` gate on IP or globally, not port), never advance the handshake to completion, and repeat as slots expire after 60s — continuously occupying the shared connection-slot pool and starving legitimate staked/unstaked connections, which will be refused via `refused_connections_too_many_open_connections` at [11](#0-10) .

### Impact Explanation
This is a remote, unauthenticated denial-of-service against the TPU QUIC ingress: a single attacker IP can occupy the entire shared `max_concurrent_connections` budget (default ~5000 slots) by keeping handshakes perpetually incomplete, causing `stats.refused_connections_too_many_open_connections` to grow and legitimate staked and unstaked clients' connections to be refused (`incoming.refuse()`), while the attacker's own attempts sit in `outstanding_incoming_connection_attempts` until the 60-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` elapses. This matches the "unbounded resource consumption / remote DoS of ingress" bounty category since it degrades or denies transaction ingestion for the validator's TPU.

### Likelihood Explanation
Highly feasible: it requires no stake, no valid keypair, and no completion of any cryptographic handshake — only sending enough of the QUIC Initial flight to have `quinn` yield an `Incoming`/`Connecting` and then withholding further packets. The attacker only needs enough concurrent sockets/threads to keep ~`max_concurrent_connections` handshakes in flight (achievable from one or a few machines) and to refresh them roughly every 60 seconds to maintain the DoS continuously. No cluster-level, multi-client, or timing constraints beyond straightforward UDP traffic generation are needed.

### Recommendation
Bound resource consumption of *incomplete* handshakes per source IP, independent of the post-handshake per-IP/global limiters, e.g.:
- Track outstanding (pre-handshake) connection attempts per IP with their own cap (e.g., a `HashMap<IpAddr, AtomicUsize>` or reuse the token-bucket abstraction gated on attempt initiation rather than success), rejecting further attempts from an IP that already has N in-flight, uncompleted handshakes.
- Alternatively, reserve only a small fraction of `max_concurrent_connections` for any single source IP's in-flight (pre-verification) handshakes, so no single IP can consume the entire shared pool.
- Consider reducing `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` specifically for the pre-verification phase, or enabling QUIC's retry/address-validation mechanism so that completing a lightweight stateless-retry round trip (which does prove IP control without a full TLS handshake) is required before a `ClientConnectionTracker` slot is granted.

### Proof of Concept
Integration test plan (in `streamer/src/nonblocking/quic.rs` test module, using existing helpers like `setup_quic_server`/`SpawnTestServerResult`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_incomplete_handshake_flood_exhausts_max_concurrent_connections() {
    let SpawnTestServerResult { server_address, stats, cancel, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig::default());

    // 1. Determine max_concurrent_connections from the spawned server result.
    // 2. Open `max_concurrent_connections` UDP sockets to server_address, each sending
    //    only a raw QUIC Initial packet (or using a quinn Endpoint that starts
    //    connecting but is dropped/paused before the handshake completes),
    //    without ever completing the TLS handshake.
    // 3. Assert stats.outstanding_incoming_connection_attempts reaches max_concurrent_connections.
    // 4. Attempt one legitimate client connection (make_client_endpoint) and assert it is
    //    refused: stats.refused_connections_too_many_open_connections increments and the
    //    legitimate connect() call fails/times out.
    // 5. Assert stats.connection_rate_limited_per_ipaddr and
    //    stats.connection_rate_limited_across_all remain 0 for the attacker's flood,
    //    demonstrating the rate limiters never triggered because no handshake completed.

    cancel.cancel();
}
```
Expected assertions: `refused_connections_too_many_open_connections > 0` for the legitimate client while attacker connections remain in `outstanding_incoming_connection_attempts` (pending) or eventually increment `connection_setup_timeout` after 60s, and `connection_rate_limited_per_ipaddr` / `connection_rate_limited_across_all` stay at 0, proving the per-IP/global limiters were bypassed.

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

**File:** streamer/src/nonblocking/quic.rs (L470-476)
```rust
{
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```

**File:** streamer/src/quic.rs (L46-48)
```rust
pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

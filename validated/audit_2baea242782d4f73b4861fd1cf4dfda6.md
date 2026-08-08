### Title
Unstaked attacker can bypass the QUIC accept-stage rate limiters and monopolize `max_concurrent_connections`, causing `refused_connections_too_many_open_connections` DoS - ([File: streamer/src/nonblocking/quic.rs])

### Summary
In `run_server`, both the overall and per-IP QUIC connection rate limiters are only *peeked* (`current_tokens()` / `is_allowed()`) at accept time and are only actually *consumed* (`consume_tokens()` / `register_connection()`) in `setup_connection` after a handshake successfully completes. An attacker who never lets the handshake finish (stalling until `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`) never debits either limiter, so they can repeatedly pass the "burst protection" checks and drive `ClientConnectionTracker::new` to exhaust `qos.max_concurrent_connections()`, refusing legitimate connections.

### Finding Description
The accept loop in `run_server` (`streamer/src/nonblocking/quic.rs:304-411`) processes each incoming QUIC `Initial` as follows:
1. `overall_connection_rate_limiter.current_tokens() == 0` — a non-consuming peek [1](#0-0) .
2. `rate_limiter.is_allowed(&incoming.remote_address().ip())` — also a non-consuming peek via `ConnectionRateLimiter::is_allowed`, which only checks `current_tokens(ip) > 0` and explicitly documents that unconfirmed IPs are not to be debited [2](#0-1) .
3. `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())` — this is the only check that mutates global state before the handshake: it unconditionally increments `stats.open_connections` and only fails (incrementing `refused_connections_too_many_open_connections`) if the global cap is already exceeded [3](#0-2) .
4. `setup_connection` is spawned, which awaits `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` — 2 seconds [4](#0-3) , [5](#0-4) .
5. Only **after** the QUIC handshake actually completes does the code call `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)`, which are the only points that actually debit either limiter [6](#0-5) .

Because token consumption is deferred to post-handshake, an attacker who deliberately never completes the TLS handshake (e.g., sends only an `Initial` packet, or stalls mid-handshake) never decrements either rate-limiter bucket. Both `current_tokens()`/`is_allowed()` peeks will therefore continue to report tokens available indefinitely from the attacker's perspective, regardless of how many stalled attempts that IP has already made. This lets the attacker's connection attempts sail past step 1 and step 2 on every attempt.

The only actual choke point left is `ClientConnectionTracker::new`, which enforces a single **global** `max_concurrent_connections` shared by staked and unstaked peers with no per-IP partition at this stage. Because the tracker's counter is incremented immediately (before the handshake completes) and only decremented on `Drop` (i.e., after the 2s `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` fires or the connection otherwise resolves), an attacker who repeatedly opens new stalled QUIC handshakes at a rate ≥ `max_concurrent_connections / QUIC_CONNECTION_HANDSHAKE_TIMEOUT` can keep `stats.open_connections` pinned at the cap continuously. Every other incoming connection — including from staked, legitimate senders — will then fail `ClientConnectionTracker::new` and be refused via `incoming.refuse()`, incrementing `refused_connections_too_many_open_connections` [7](#0-6) .

The code comment even acknowledges the deliberate design tradeoff of not debiting per-IP/global limiters pre-handshake ("before a peer has asserted control over their ip address... employ... a global rate-limiter... rate-limit abusive peers by (control-asserted) ip") [8](#0-7) , but this reasoning only explains why *per-IP* debiting is deferred to avoid spoofing collateral damage — it does not address that the *global* rate limiter (`overall_connection_rate_limiter`) is likewise only consumed post-handshake, and the only remaining global gate (`ClientConnectionTracker`/`max_concurrent_connections`) can be starved entirely by a single non-spoofed attacker that simply never finishes handshakes.

### Impact Explanation
This is a QoS/connection-limit evasion that lets a single unprivileged, unstaked, real (non-spoofed) client deny all other TPU QUIC connections — including staked senders — for as long as the attack is sustained, by keeping `stats.open_connections` saturated at `qos.max_concurrent_connections()`. This matches the stated bounty scope: "An unprivileged, unstaked client can bypass or unfairly capture connection... QoS limits and starve legitimate senders of TPU capacity."

### Likelihood Explanation
Feasibility is high and requires no special access: only an unstaked remote client capable of opening raw QUIC/UDP connections to the leader's public TPU port. `qos.max_concurrent_connections()` is finite (e.g., `max_staked_connections * 5 / 4` for `SimpleQos`, see `streamer/src/nonblocking/simple_qos.rs:422-425`), and `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` is fixed at 2 seconds. The attacker only needs enough concurrent stalled handshakes (from one or a few source ports/sockets on the same IP) to reach `max_concurrent_connections` and to keep re-issuing new stalled attempts as old ones expire — a modest, sustainable connection-churn rate, well within a single unprivileged client's capability, and repeatable indefinitely.

### Recommendation
Enforce the per-IP and overall connection-rate limiters (or a dedicated stalled-handshake-attempt counter) at accept time in a way that accounts for outstanding (not-yet-completed) attempts, not just completed handshakes — e.g., consume a token from `overall_connection_rate_limiter` and a per-IP "attempt" bucket when `ClientConnectionTracker::new` succeeds (pre-handshake), refunding it only if needed, so that repeated stalled attempts from the same source are throttled the same way completed connections are. Additionally, consider partitioning `max_concurrent_connections`/`ClientConnectionTracker` so that a fixed portion of "in-flight handshake" slots is reserved for previously-unseen or staked-adjacent traffic so a single unstaked source cannot exhaust the entire pool.

### Proof of Concept
Integration test plan (extending existing test infra in `streamer/src/nonblocking/quic.rs` tests / `tpu-client-next/tests/connection_workers_scheduler_test.rs`):
1. Start `setup_quic_server` with a small `qos.max_concurrent_connections()` (e.g., set `max_staked_connections` low so cap ≈ 5).
2. Spawn an attacker task that repeatedly opens raw QUIC connections to the server (using `quinn::Endpoint::connect`) but intentionally stalls the handshake (e.g., connect from a client endpoint configured to never respond to the server's handshake packets, or drop the future right after `connect()` without awaiting completion), at a rate that keeps ~`max_concurrent_connections` stalled attempts outstanding at all times, continuously re-issuing new ones as `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` expires on old ones.
3. Concurrently, attempt a legitimate `make_client_endpoint` connection (simulating a staked client) at multiple points during the attack window.
4. Assert: (a) `stats.refused_connections_too_many_open_connections` increments substantially during the attack, and (b) the legitimate client's connection attempt fails/times out (is refused) while the attack is ongoing, demonstrating that legitimate senders are perpetually starved of a connection slot despite never exceeding `rate_limiter`/`overall_connection_rate_limiter` thresholds (verify via `stats.connection_rate_limited_per_ipaddr` and `stats.connection_rate_limited_across_all` remaining at 0 or low while `refused_connections_too_many_open_connections` spikes).

### Citations

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

**File:** streamer/src/nonblocking/quic.rs (L476-508)
```rust
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

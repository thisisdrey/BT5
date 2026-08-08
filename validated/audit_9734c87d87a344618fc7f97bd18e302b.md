### Title
Never-completing QUIC handshakes bypass per-IP and global connection rate limiters, exhausting the shared connection-slot budget and causing `incoming.refuse()` for legitimate senders - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ConnectionRateLimiter::is_allowed` (checked before `incoming.accept()`) is a non-mutating peek, and the only place that actually debits the per-IP bucket — `register_connection` — plus the only place that debits the global `overall_connection_rate_limiter` — `consume_tokens` — are both invoked *inside* `setup_connection` only on the successful-handshake path. An attacker who never lets the QUIC handshake complete (holding it open until `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, 2s) never triggers either debit, so neither rate limiter ever constrains repeated attempts from the same IP or in aggregate. The only thing that actually limits concurrency for such attempts is `ClientConnectionTracker`'s `open_connections` counter versus `qos.max_concurrent_connections()`, which is peer/stake-agnostic and shared by everyone, letting an attacker saturate it and force `incoming.refuse()` on subsequent connections including from staked/legitimate peers.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:254`), for each incoming connection the code:
1. Peeks `overall_connection_rate_limiter.current_tokens() == 0` [1](#0-0) .
2. Peeks `rate_limiter.is_allowed(&ip)` — a **read-only** check against `KeyedRateLimiter::current_tokens` [2](#0-1) .
3. Calls `ClientConnectionTracker::new`, which increments `open_connections` and rejects (causing `incoming.refuse()`) only once `open_connections >= max_concurrent_connections` [3](#0-2) .
4. Increments `outstanding_incoming_connection_attempts` and spawns `setup_connection`, which awaits the handshake for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) [4](#0-3) [5](#0-4) .

Only on a **successful** handshake does `setup_connection` call `rate_limiter.register_connection(&from.ip())` (the mutating per-IP debit) and `overall_connection_rate_limiter.consume_tokens(1)` (the mutating global debit) [6](#0-5) . If the handshake times out or errors, `outstanding_incoming_connection_attempts` is decremented and the function returns without ever calling `register_connection` or `consume_tokens` [7](#0-6) [8](#0-7) .

Consequently, an attacker who deliberately never lets the handshake finish (e.g., sends an Initial packet and stalls, or trickles Handshake data so it never resolves before the 2s timeout) can repeatedly reopen connections from the same source IP without ever being throttled by `is_allowed`/`register_connection`, and without ever draining the global token bucket. The only real, functioning cap on such stalled connections is the shared, stake-agnostic `open_connections` vs `max_concurrent_connections` gate in `ClientConnectionTracker`, where `max_concurrent_connections()` is deliberately sized as only `(max_staked_connections + max_unstaked_connections) * 5 / 4` — "Allow 25% more connections than required to allow for handshake" [9](#0-8) . By keeping enough concurrent half-open connections alive (cycling every ~2s, refilled as slots free up), the attacker can consume this shared headroom, causing `ClientConnectionTracker::new` to fail for subsequent accepts — including from legitimate staked peers, since peer stake/type is unknown until after the handshake completes — resulting in `incoming.refuse()` for those connections [10](#0-9) .

Note: the `run_server` `select!` accept loop (`accepts.next()`) itself is not literally blocked by outstanding `setup_connection` tasks — `TaskTracker::spawn` is non-blocking and `TaskTracker` has no fixed slot limit, so new endpoint-level accepts continue to be polled promptly. The actual starvation mechanism is the shared `open_connections`/`max_concurrent_connections` gate being exhausted, not a stalled accept loop or a bounded `TaskTracker`.

### Impact Explanation
This is a QoS-evasion / connection-admission DoS: an unstaked, unauthenticated attacker can bypass the per-IP and global connection-rate limiters that are specifically designed ("rate-limit abusive peers by ip", "protect against connection attempt bursts with a global rate-limiter" — see comment at `streamer/src/nonblocking/quic.rs:332-340`) to stop exactly this kind of abuse, and instead compete only against the coarse, stake-agnostic 25% handshake-headroom slot pool. Sustained abuse can starve legitimate senders (staked or unstaked) of new TPU QUIC connections for the duration of the attack, matching the "QoS evasion / TPU capacity starvation" bounty category.

### Likelihood Explanation
Feasible and repeatable with a single unprivileged client (or a small number of source IPs) that connects to the leader's public TPU QUIC port and intentionally avoids completing the handshake within 2 seconds, then repeats. No stake, keys, or special config are required — the attacker is exactly the unprivileged, unstaked remote client described in scope. Amplification is limited only by the attacker's own outbound connection rate, since defenses that should limit exactly this (per-IP/global rate limiters) never engage on the non-completing path.

### Recommendation
Debit (or at least provisionally reserve) the per-IP and global connection-rate-limiter tokens at connection-*attempt* time (before/at `incoming.accept()`), not only on handshake success, so that a peer/IP that never completes the handshake is still charged for the attempt. Alternatively, cap the number of *outstanding* (not-yet-handshaked) connections per IP separately from `max_concurrent_connections`, and/or reduce `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` exposure by requiring proof-of-ownership (e.g., stateless retry) before consuming a slot from the shared 25% headroom pool.

### Proof of Concept
Integration test plan (extending existing test harness in `streamer/src/nonblocking/testing_utilities.rs` / `streamer/src/nonblocking/quic.rs` tests):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_stalled_handshakes_bypass_rate_limiters_and_starve_legit_conn() {
    // Spawn server with small SwQosConfig (max_staked=1, max_unstaked=4) so
    // max_concurrent_connections() == (1+4)*5/4 == 6, easy to saturate.
    let SpawnTestServerResult { server_address, stats, cancel, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig { 
            max_staked_connections: 1, max_unstaked_connections: 4, ..Default::default() 
        });

    // Attacker opens `max_concurrent_connections` raw UDP "connections" that send an
    // Initial packet but never complete the QUIC handshake (e.g. custom endpoint that
    // sends garbage/partial crypto frames), from the same or few IPs.
    for _ in 0..6 {
        spawn_stalled_handshake_attempt(server_address).await; // never resolves
    }

    // Give slots time to fill (< QUIC_CONNECTION_HANDSHAKE_TIMEOUT).
    sleep(Duration::from_millis(200)).await;

    // Assert per-IP rate limiter was never actually decremented for the attacker IP
    // (is_allowed still true) because register_connection was never reached.
    assert_eq!(stats.connection_rate_limited_per_ipaddr.load(Ordering::Relaxed), 0);

    // A legitimate client now attempts to connect and should succeed within a bounded
    // latency envelope; assert it instead gets refused due to exhausted
    // open_connections/max_concurrent_connections budget.
    let legit_conn = make_client_endpoint(&server_address, None).await;
    // Expect assertion failure demonstrating starvation:
    assert_ne!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed), 0);
}
```
Expected result on current code: `connection_rate_limited_per_ipaddr == 0` for the attacker's repeated stalled attempts (confirming the per-IP limiter bypass), and `refused_connections_too_many_open_connections` increments for the legitimate client, demonstrating starvation via the shared `open_connections`/`max_concurrent_connections` budget rather than the intended per-IP/global rate limiters.

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

**File:** streamer/src/nonblocking/quic.rs (L381-399)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

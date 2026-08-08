### Title
Attacker can monopolize the QUIC handshake headroom to permanently deny all new TPU connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`SwQos::max_concurrent_connections` grants a fixed 25% headroom over `max_staked_connections + max_unstaked_connections` for in-flight handshakes, guarded only by `ClientConnectionTracker::new` against the global `open_connections` counter. Because the per-IP and global connection-rate limiters (`rate_limiter.register_connection` / `overall_connection_rate_limiter.consume_tokens`) are only invoked *after* a handshake successfully completes, an attacker who never completes the handshake never gets rate-limited, and can keep the tracker count pinned at the cap for the full `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) window, indefinitely refusing all subsequent connections including staked ones.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:331-379`), for each incoming QUIC `Incoming`:
1. `total_incoming_connection_attempts` is bumped.
2. `overall_connection_rate_limiter.current_tokens() == 0` is checked (a non-mutating peek, not a consume) [1](#0-0) .
3. `rate_limiter.is_allowed(&ip)` is checked, which for an IP with no prior *registered* connection returns `true` unconditionally (`None => true`) [2](#0-1) .
4. `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` is called, incrementing the global `open_connections` counter and admitting the connection if `open_connections < max_concurrent_connections()` [3](#0-2) .

Only after this admission does `setup_connection` await the QUIC handshake with a 2-second timeout (`QUIC_CONNECTION_HANDSHAKE_TIMEOUT`) [4](#0-3) [5](#0-4) . Crucially, `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` — the actual mutating rate-limit consumption — only happen in the `Ok(new_connection)` branch, i.e. after the handshake succeeds [6](#0-5) . An attacker who sends Initial packets but never completes the TLS handshake never triggers these consuming calls, so neither the per-IP nor the global connection-rate limiter is ever actually charged for stalled/incomplete handshakes.

`SwQos::max_concurrent_connections` computes the shared global cap as `(max_staked_connections + max_unstaked_connections) * 5 / 4` [7](#0-6) , and this single counter (`stats.open_connections`) is shared across all connections regardless of stake — there is no reserved per-class headroom. By sustaining just enough parallel stalled handshakes to hit this cap and refreshing them faster than the 2-second handshake timeout expires them, an attacker keeps `ClientConnectionTracker::new` failing for everyone, causing legitimate connection attempts (staked or unstaked) to be refused via `refused_connections_too_many_open_connections` at `streamer/src/nonblocking/quic.rs:371-379`.

### Impact Explanation
This is a cluster-wide denial-of-new-TPU-connections condition: once the headroom is saturated, `ClientConnectionTracker::new` rejects every new connection attempt — staked senders included — until the attacker stops or is rate-limited by network-layer controls outside this code path. This matches the Agave bounty category of a validator/leader DoS reachable by a single unstaked network attacker with no special privileges, since the specific defense mechanisms intended to bound this (per-IP/global connection rate limiters) do not apply to never-completed handshakes.

### Likelihood Explanation
Preconditions are exactly as stated: an unprivileged remote attacker capable of sending QUIC Initial packets to the TPU port and simply not completing (or artificially stalling) the handshake, sustaining occupancy for `(max_staked_connections + max_unstaked_connections) * 5 / 4` slots, refreshing before each occupant's 2-second timeout expires. No IP spoofing, staking, or gossip control is required — only enough parallel sockets/ports from the attacker's own address(es) to reach the target concurrency, which is a modest number derived from cluster defaults for staked/unstaked connection caps. This is straightforwardly repeatable and recovers automatically only once the attacker relents (transient, not permanent, but sustained for as long as attacker maintains the flood).

### Recommendation
Rate-limit or track "handshake attempts" (not just successful connections) per-IP and globally before/while admitting into the `ClientConnectionTracker`, e.g., call `rate_limiter.is_allowed`/consume-equivalent checks that account for pending (not-yet-verified) attempts, or reserve/charge tokens at `Incoming` time and refund them if handshake fails, rather than only charging on success. Additionally, consider partitioning the headroom so that unstaked/no-address-verified handshakes cannot starve the pool used for legitimate staked reconnections (e.g., separate counters or reserved slots for validated peers, or shrinking headroom exposure via QUIC's stateless-retry address validation before consuming a slot).

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs test module (integration test)
#[tokio::test(flavor = "multi_thread")]
async fn test_stalled_handshakes_exhaust_headroom_and_deny_staked_client() {
    // Setup server with small caps to make the test tractable:
    // SwQosConfig { max_staked_connections: 4, max_unstaked_connections: 4, .. }
    // max_concurrent_connections() == (4+4)*5/4 == 10
    let SpawnTestServerResult { server_address, stats, cancel, .. } = setup_quic_server(
        None,
        QuicStreamerConfig::default_for_tests(),
        SwQosConfig { max_staked_connections: 4, max_unstaked_connections: 4, ..Default::default() },
    );

    // Attacker: open raw UDP sockets and send just enough bytes to look like
    // QUIC Initial packets (or use quinn::Endpoint::connect but never drive
    // the handshake to completion / drop the future before it resolves)
    // for exactly 10 concurrent attempts, refreshing every < 2s to keep
    // ClientConnectionTracker slots occupied indefinitely.
    let attacker_futures = spawn_stalled_handshakes(server_address, /*count=*/10);

    // Wait for open_connections to hit the cap.
    wait_until(|| stats.open_connections.load(Ordering::Relaxed) >= 10);

    // Legitimate staked client attempt should now be refused.
    let staked_keypair = Keypair::new(); // registered with stake in staked_nodes
    let result = make_client_endpoint(&server_address, Some(&staked_keypair)).await
        .connect_and_open_stream().await;
    assert!(result.is_err());
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0);

    // Recovery: once attacker stops refreshing, after QUIC_CONNECTION_HANDSHAKE_TIMEOUT (2s)
    // the stalled trackers are dropped and legitimate connections succeed again.
    drop(attacker_futures);
    tokio::time::sleep(Duration::from_secs(3)).await;
    let result2 = make_client_endpoint(&server_address, Some(&staked_keypair)).await
        .connect_and_open_stream().await;
    assert!(result2.is_ok());

    cancel.cancel();
}
```
Expected assertions: while the attacker sustains ≥`max_concurrent_connections()` stalled handshakes, a legitimate staked client's connection is refused (`refused_connections_too_many_open_connections` increments); after the attacker stops and `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` elapses, connections succeed again — confirming the DoS window is fully attacker-controlled and requires no staked/privileged capability.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L80-80)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L471-476)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
```

**File:** streamer/src/nonblocking/quic.rs (L478-510)
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

                stats.total_new_connections.fetch_add(1, Ordering::Relaxed);
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

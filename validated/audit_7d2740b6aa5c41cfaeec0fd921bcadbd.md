### Title
Unbounded pre-handshake connection slot exhaustion via slow-handshake flooding from a single IP - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ClientConnectionTracker::new` enforces only a *global* concurrency cap (`max_concurrent_connections = (max_staked + max_unstaked) * 5/4`) before a peer has completed its TLS handshake or been placed into either `ConnectionTable`. The only per-IP gate applied before this counter is incremented, `ConnectionRateLimiter::is_allowed`, is a no-op for any IP that has never completed a handshake, because token consumption only happens in `register_connection` *after* the handshake succeeds. A single unstaked attacker can therefore open many QUIC connections, stall them mid-handshake, and occupy the entire 25% headroom reserved for in-flight handshakes, starving legitimate (including staked) peers.

### Finding Description
In `run_server`'s accept loop (`streamer/src/nonblocking/quic.rs`), for every incoming connection:
1. `overall_connection_rate_limiter` (global token bucket) is checked.
2. `rate_limiter.is_allowed(&ip)` is checked — this only inspects `KeyedRateLimiter::current_tokens(ip)`, and returns `true` unconditionally when the IP has no existing record [1](#0-0) .
3. `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` is called, which only bumps and bounds the *global* `stats.open_connections` atomic counter [2](#0-1) .
4. Only then is `setup_connection` spawned, which awaits the QUIC handshake under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, and it is *only after the handshake completes* that `rate_limiter.register_connection(&from.ip())` actually consumes a per-IP token [3](#0-2) .

Because `register_connection` (the only place tokens are consumed) runs after handshake completion, an attacker who never lets the handshake finish (slow/throttled ACKs, or simply never sending the final handshake message) never gets rate-limited by IP. Each such connection still increments `stats.open_connections` via `ClientConnectionTracker::new` and holds that slot for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` before the timeout branch runs and the tracker is dropped, decrementing the counter [4](#0-3) [5](#0-4) .

Since `max_concurrent_connections()` for the stake-weighted QoS controller is exactly `(max_staked_connections + max_unstaked_connections) * 5 / 4` [6](#0-5) , and this is the *only* structure guarding pre-registration connections (no `ConnectionEntry`/`ConnectionTable` accounting occurs until after `try_add_connection` is called post-handshake in `setup_connection`), a client that continuously opens new slow-handshake connections from one IP (re-dialing as old attempts time out, to stay under the global `overall_connection_rate_limiter`) can keep the entire 25% headroom permanently occupied. Once `stats.open_connections >= max_concurrent_connections`, every subsequent `Incoming` — including from staked, high-value peers — is refused at `incoming.refuse()` and counted as `refused_connections_too_many_open_connections` [2](#0-1) .

The pre-existing mitigations (`overall_connection_rate_limiter`, `rate_limiter.is_allowed`, and the handshake timeout) do not close this gap: the global rate limiter only throttles the *rate* of new attempts, not the standing occupancy of the pool, and the per-IP limiter is inert until a handshake succeeds — which the attacker deliberately avoids.

### Impact Explanation
This is a total connection-slot exhaustion at the TPU QUIC ingress: once the handshake-headroom pool is saturated by an unprivileged attacker, `run_server`'s accept loop refuses all new incoming connections (`refused_connections_too_many_open_connections`), including from staked validators/leaders that would otherwise be admitted into `staked_connection_table`. This matches the Agave bounty category of "QoS evasion / DoS against transaction ingress" since it denies all new TPU traffic sources, staked and unstaked alike, without requiring any staked/privileged capability.

### Likelihood Explanation
Feasible and repeatable with only unprivileged network access: the attacker needs a real (non-spoofed) IP capable of completing the initial retry-validated QUIC handshake negotiation but can artificially slow-walk the remaining handshake steps (or simply stop responding) to stay "in-flight" up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`. The attacker only needs to sustain a throughput of new connection attempts equal to `max_concurrent_connections()/QUIC_CONNECTION_HANDSHAKE_TIMEOUT` to keep the pool full indefinitely, which is well within what the `overall_connection_rate_limiter` permits (it is sized for legitimate high-throughput connection turnover, not to prevent slow-handshake occupancy). No staked identity, gossip control, or multiple source IPs are required — a single IP suffices since the per-IP limiter never engages against a peer that never finishes a handshake.

### Recommendation
Introduce accounting/rate-limiting of in-flight (pre-handshake) connections per source IP — e.g., track outstanding un-completed handshakes per IP address (similar to `outstanding_incoming_connection_attempts` but keyed by IP) and cap it independently of the global `max_concurrent_connections`, or consume a per-IP token at `Incoming` accept time (before/instead of only at successful handshake completion) so that `register_connection`-style accounting also limits *concurrent pending* attempts, not just completed ones.

### Proof of Concept
Integration test plan (Rust, `tokio::test`, using the existing `spawn_stake_weighted_qos_server` / `setup_quic_server` test harness in `streamer/src/nonblocking/quic.rs`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_slow_handshake_exhausts_handshake_headroom() {
    // spawn server with small max_staked/max_unstaked_connections, e.g. 4 each,
    // so max_concurrent_connections() == (4+4)*5/4 == 10
    let SpawnTestServerResult { server_address, stats, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig { max_staked_connections: 4, max_unstaked_connections: 4, ..Default::default() });

    // Open raw UDP "Initial" packets / partially-driven quinn Connecting futures
    // from a single source IP, never finishing the handshake (drop the client
    // endpoint's send task after the first flight, or use a custom UDP socket
    // that only sends the QUIC Initial and stalls).
    for _ in 0..10 {
        spawn_stalled_handshake_from(server_address).await;
    }

    // A legitimate, fully-handshaking client from a *different* IP/loopback alias
    // should now be refused.
    let legit = make_client_endpoint(&server_address, None).await;
    assert!(legit_connection_is_refused(&legit).await);
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0);
}
```
Expected assertion: `stats.refused_connections_too_many_open_connections` increments and a legitimate client's connection is refused, while none of the 10 attacker connections ever appear in `staked_connection_table`/`unstaked_connection_table` (no `ConnectionEntry` committed), demonstrating slot exhaustion purely from pre-registration handshake occupancy.

### Citations

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

**File:** streamer/src/nonblocking/quic.rs (L229-234)
```rust
impl Drop for ClientConnectionTracker {
    /// When this is dropped, reduce the open connection count.
    fn drop(&mut self) {
        self.stats.open_connections.fetch_sub(1, Ordering::Relaxed);
    }
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

**File:** streamer/src/nonblocking/quic.rs (L472-493)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

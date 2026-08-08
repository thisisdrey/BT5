### Title
Unstaked flood of stalled QUIC handshakes exhausts the shared `ClientConnectionTracker` slot pool and starves legitimate staked connections - (File: `streamer/src/nonblocking/quic.rs`)

### Summary
`ClientConnectionTracker::new` enforces a single global counter (`stats.open_connections`) against `qos.max_concurrent_connections()` for *every* incoming QUIC connection, regardless of whether the peer will ultimately be staked or unstaked, because peer stake is unknown until after the TLS handshake completes. Critically, the only defense that could throttle a single attacker's flood during this pre-handshake window — the overall global token bucket — is source-agnostic, and the per-IP `ConnectionRateLimiter` is not consulted until *after* a handshake succeeds, so an attacker can hold many slots open for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` with no per-source cap on the shared pool.

### Finding Description
In the accept loop, every `Incoming` connection first passes the global `overall_connection_rate_limiter` check and a *read-only* `rate_limiter.is_allowed(ip)` check [1](#0-0) . `ConnectionRateLimiter::is_allowed` only inspects existing token-bucket state and, for any IP it hasn't "seen" yet (i.e., has not completed a handshake), unconditionally returns `true` [2](#0-1) . Only `register_connection`, which actually consumes a token for that IP, is source-limiting — but it is invoked only *after* the QUIC handshake completes successfully, inside `setup_connection` [3](#0-2) .

After the two rate checks, the code takes a slot from the single, unpartitioned counter:
```
let Ok(client_connection_tracker) =
    ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())
else {
    stats.refused_connections_too_many_open_connections.fetch_add(1, ...);
    incoming.refuse();
    continue;
};
``` [4](#0-3) 

`ClientConnectionTracker::new` increments `stats.open_connections` and fails only once that raw counter reaches `max_concurrent_connections` [5](#0-4) . This counter is not partitioned by staked/unstaked at all — it is decremented only on `Drop` of the tracker, which happens either when the connection is torn down or when the handshake times out after `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds, not 60 — that 60s constant belongs to the client side in `quic-client/src/nonblocking/quic_client.rs`) [6](#0-5) [7](#0-6) . The capacity itself is computed from combined staked+unstaked limits: `self.config.max_staked_connections * 5 / 4` in `SimpleQos::max_concurrent_connections` (and analogous logic in `SwQos`) [8](#0-7) .

Because per-IP consumption is deferred to *after* the handshake, an attacker never has to "pay" the per-IP rate limiter as long as connections are deliberately stalled mid-handshake (e.g. never completing the TLS ClientHello/Finished exchange, or an attacker client that sends Initial packets and then goes silent). The only cost gating the attack is the source-agnostic `overall_connection_rate_limiter` (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, burst `MAX_CONNECTION_BURST = 1000`) [9](#0-8) , which any high-throughput unstaked client can approach by opening many parallel half-open connections from one or a few source addresses (address-validation via QUIC retry prevents pure IP spoofing, but does not prevent a real client from opening thousands of genuine sockets). Sustaining a fill rate above `max_concurrent_connections() / QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (≈ capacity/2s) keeps the shared `open_connections` counter pinned at capacity, so every subsequent `ClientConnectionTracker::new` call — including for a legitimate staked leader's incoming handshake — fails and increments `refused_connections_too_many_open_connections`, and `incoming.refuse()` is issued [4](#0-3) .

### Impact Explanation
This is a full TPU ingress DoS: even a well-staked leader cannot begin a QUIC handshake against the node while the shared, unpartitioned handshake-slot pool is saturated by unstaked/anonymous traffic, because stake-based prioritization (staked vs. unstaked connection tables, pruning, stream throttling) only takes effect *after* a connection is admitted into `qos.try_add_connection`, which happens strictly after `ClientConnectionTracker::new` succeeds. This maps to the Agave bounty category of QoS/stake-weighted-QoS evasion leading to denial of service against staked traffic.

### Likelihood Explanation
Feasibility depends on an attacker being able to sustain a connection-attempt rate close to the global `overall_connection_rate_limiter` ceiling (2500/sec, burst 1000) while keeping each connection unresolved for close to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s). This is achievable from a single well-resourced unstaked client (many ephemeral sockets/QUIC "Connecting" attempts that never complete Handshake), and does not require IP spoofing, staked identity, or multiple distinct machines. The repeated cycle (open half-open connection → wait ~2s for timeout/drop → repeat) is straightforward to automate and reproduce.

### Recommendation
Partition the pre-authentication handshake-slot budget so unstaked flooding cannot exhaust capacity reserved for staked handshakes — e.g., maintain separate `open_connections` counters (or reserved sub-quotas) for staked-in-progress vs. unstaked-in-progress handshakes, or reserve a minimum fraction of `max_concurrent_connections()` exclusively for connections whose remote pubkey matches a currently-staked identity (checked via TLS client cert prior to full handshake completion where possible). Additionally, consume per-IP `ConnectionRateLimiter` tokens at `Incoming` accept time (before consuming a `ClientConnectionTracker` slot), not only after successful handshake, so an unauthenticated source cannot hold many pre-handshake slots without being throttled per source.

### Proof of Concept
Integration test plan (extends existing test patterns in `streamer/src/nonblocking/quic.rs`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_unstaked_flood_starves_staked_handshake() {
    // Spawn server with a small max_concurrent_connections capacity, e.g.
    // SwQosConfig { max_staked_connections: 4, max_unstaked_connections: 4, .. }
    // so capacity = (4+4)*5/4 = 10.
    let SpawnNonBlockingServerResult { stats, .. } = spawn_stake_weighted_qos_server(..);

    // Attacker: open N ~= capacity connections that send an Initial packet
    // but never complete the handshake (e.g. raw UDP client sending garbage
    // QUIC long-header Initial packets to the server port, or a quinn client
    // endpoint whose connect() future is intentionally left pending/dropped
    // mid-handshake), repeated fast enough to always keep `open_connections`
    // at/near `max_concurrent_connections()` before QUIC_CONNECTION_HANDSHAKE_TIMEOUT
    // (2s) elapses on each.
    spawn_flood_of_stalled_handshakes(server_address, capacity_estimate).await;

    // Legitimate staked client attempts to connect.
    let staked_keypair = Keypair::new();
    // stake it via staked_nodes map used by the server ...
    let client_conn_result = make_client_endpoint_with_keypair(&server_address, &staked_keypair).await;

    // Assert refusal purely due to attacker load.
    assert!(client_conn_result.is_err());
    assert!(
        stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0
    );
}
```
Expected assertion: `stats.refused_connections_too_many_open_connections` increments and the staked client's connection attempt fails/times out, even though the staked client itself never exceeded any per-IP or per-peer quota — demonstrating that the shared, unpartitioned `open_connections` counter (bounded by `qos.max_concurrent_connections()`) is exhaustible by unstaked traffic alone.

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

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```

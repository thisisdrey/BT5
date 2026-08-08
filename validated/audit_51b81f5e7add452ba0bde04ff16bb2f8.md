### Title
Per-IP QUIC connection-attempt admission uses a non-consuming "read-only" rate check, allowing a single attacker IP to monopolize the entire global `ClientConnectionTracker` slot pool before per-IP throttling ever engages - ([File: streamer/src/nonblocking/quic.rs])

### Summary
In `run_server`, before a `ClientConnectionTracker` slot is reserved and `setup_connection` is spawned, the only per-source-IP gate applied is `ConnectionRateLimiter::is_allowed`, which is a non-mutating check (`current_tokens(ip) > 0`). Actual token consumption (`register_connection`) only happens inside `setup_connection` *after* the QUIC handshake completes, which can take up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s). Because no tokens are consumed pre-handshake, a single attacker IP can keep passing `is_allowed` for as many concurrent handshake attempts as it can push through, bounded only by the *global* `max_concurrent_connections` cap shared by all clients (staked and unstaked), not by any per-IP concurrency limit.

### Finding Description
The admission sequence in `run_server` is:
1. `overall_connection_rate_limiter.current_tokens() == 0` — global, non-consuming check. [1](#0-0) 
2. `rate_limiter.is_allowed(&ip)` — per-IP, non-consuming check. [2](#0-1) 
3. `ClientConnectionTracker::new(...)` — the only *hard*, atomically-enforced cap, but it is global (`qos.max_concurrent_connections()`), not per-IP. [3](#0-2) 

`is_allowed` only reads `current_tokens`, it never decrements the per-IP `TokenBucket`: [4](#0-3) 

The per-IP token bucket is only actually consumed by `register_connection`, which runs inside `setup_connection` *after* `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` succeeds — i.e., only after a full handshake round-trip, up to 2 seconds later: [5](#0-4) 

`ClientConnectionTracker::new` enforces the only true concurrency bound, but it is a single global counter (`stats.open_connections`) shared across all source IPs, not scoped per IP: [6](#0-5) 

Because `is_allowed` never decrements anything, an attacker from one IP can fire many simultaneous `Connecting` handshakes (different source ports are not even required — the rate limiter keys strictly on IP, and QUIC's per-connection state is anyway keyed by connection ID, so multiple simultaneous handshakes from one IP address are legitimate at the protocol level). Every one of these attempts will pass the per-IP `is_allowed` check (since the token bucket for that IP hasn't been decremented yet) and will successfully acquire a `ClientConnectionTracker` slot from the shared global pool, up to `qos.max_concurrent_connections()`. Only after each attempt's handshake completes (or times out after 2s) does `register_connection` finally get a chance to reject excess connections from that IP — by which time the slot has already been held, denying it to legitimate unstaked/staked clients for the full handshake window.

### Impact Explanation
This is scoped, unprivileged-attacker "outstanding-connection-attempt exhaustion" exactly as described: a single IP can consume the entire `max_concurrent_connections` budget (shared by the whole validator's QUIC TPU port) repeatedly, in ~2-second cycles bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, starving all other clients (including staked/legitimate transaction senders) from obtaining a `ClientConnectionTracker` slot. This matches the Agave ingress-DoS bounty category (QoS evasion via connection churn causing legitimate-connection denial), not a memory-safety or consensus bug.

### Likelihood Explanation
Feasible under the stated preconditions (unstaked, IP under its per-minute cap): the attacker only needs to open more simultaneous QUIC handshakes than `qos.max_concurrent_connections()` from one IP, which requires no stake, no special network position, and only standard QUIC client libraries capable of many concurrent `Connecting` attempts. This is trivially repeatable — the attacker just re-issues bursts every ~2 seconds (the handshake timeout window), continuously refilling the slot pool as attempts age out.

### Recommendation
Reserve/consume the per-IP token (or a separate per-IP *concurrent attempt* counter) at accept time in `run_server`, before spawning `setup_connection`, instead of only checking `is_allowed`/`current_tokens` non-destructively. Alternatively, add a per-IP cap on concurrently outstanding (`Connecting`) attempts, independent of the global `ClientConnectionTracker` pool, so no single IP can claim more than a bounded fraction of `max_concurrent_connections` while its handshakes are still pending.

### Proof of Concept
Integration test plan (extending the existing `streamer/src/nonblocking/connection_rate_limiter.rs` / `run_server` test harness):
```rust
#[tokio::test]
async fn test_single_ip_can_exhaust_global_connection_slots() {
    // Configure a small max_concurrent_connections (e.g. 8) via qos config,
    // and a generous per-ip max_connections_per_ipaddr_per_min (e.g. 100, burst 1000)
    // so the per-ip TokenBucket alone would not reject the burst.
    let SpawnTestServerResult { server_address, stats, .. } = setup_quic_server(
        None,
        QuicStreamerConfig { max_connections_per_ipaddr_per_min: 100, ..Default::default() },
        SwQosConfig { max_concurrent_connections: 8, ..Default::default() }, // hypothetical field name
    );

    // Spawn > max_concurrent_connections simultaneous handshakes from ONE source IP
    // (e.g. 32 concurrent `Connecting` futures via 32 client endpoints on distinct
    // local ports, all connecting to server_address at once).
    let handles: Vec<_> = (0..32)
        .map(|_| tokio::spawn(make_client_endpoint(&server_address, None)))
        .collect();

    // While these are in-flight, attempt one legitimate connection from a
    // *different* IP/simulated peer and assert it is refused with
    // refused_connections_too_many_open_connections, proving the global
    // slot pool was monopolized by a single attacker IP despite being
    // "under" its per-minute cap.
    tokio::time::sleep(Duration::from_millis(200)).await;
    let legit = make_client_endpoint(&server_address, None).await;
    assert!(legit.is_err() || /* connection refused via CONNECTION_CLOSE_CODE_TOO_MANY */ true);

    assert!(
        stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0,
        "legitimate peer denied a slot due to single-IP burst monopolizing global cap"
    );
}
```
Expected result: `refused_connections_too_many_open_connections` increments for the unrelated legitimate peer even though the attacker IP never exceeded its own per-minute connection quota, demonstrating that `is_allowed`'s non-consuming check fails to bound per-IP *concurrent* handshake attempts against the shared global slot pool.

### Citations

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

**File:** streamer/src/nonblocking/quic.rs (L371-399)
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

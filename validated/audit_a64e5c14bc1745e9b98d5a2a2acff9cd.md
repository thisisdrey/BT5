### Title
Per-IP connection rate limiter can be bypassed by never completing the QUIC handshake, allowing unlimited half-open connection churn from a single IP - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ConnectionRateLimiter::is_allowed` only rejects an IP if a prior *completed* handshake already created a token-bucket record via `register_connection`. Since `register_connection` is only invoked in `setup_connection` after a successful handshake [1](#0-0) , an attacker who always aborts before the handshake completes never creates a record for their IP, so `is_allowed` keeps returning `true` forever for that IP [2](#0-1) . This lets a single unstaked IP repeatedly consume slots in the shared, global `open_connections`/`outstanding_incoming_connection_attempts` pool with no per-IP throttling.

### Finding Description
In `run_server`, incoming connections are checked in this order: global overall rate limiter (`overall_connection_rate_limiter.current_tokens()`, a check-only, non-consuming read when unfulfilled handshakes are involved) [3](#0-2) , then per-IP `rate_limiter.is_allowed(...)` [4](#0-3) , then the global concurrency cap via `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` [5](#0-4) . Only after passing these does the code increment `outstanding_incoming_connection_attempts` and call `incoming.accept()` to spawn `setup_connection` [6](#0-5) .

`register_connection`, the only call that mutates the per-IP `ConnectionRateLimiter` state, is invoked exclusively inside `setup_connection` and only in the `Ok(new_connection)` branch after the QUIC handshake (`connecting.await`) succeeds within the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` [7](#0-6) . If the attacker aborts before the handshake completes (e.g., drops the socket, sends garbage after Initial, or simply never responds), `setup_connection` hits the timeout branch and only decrements `outstanding_incoming_connection_attempts` and drops `ClientConnectionTracker` (decrementing `open_connections`) — `register_connection` is never called [8](#0-7) [9](#0-8) .

Because `is_allowed` treats "no record" as `true` (allowed) [2](#0-1) , and no record is ever created for an IP that never completes a handshake, the per-IP limiter (`DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE` = 8, burst 80) [10](#0-9)  imposes **zero** restriction on this attacker. The only remaining guard is the shared, IP-agnostic `max_concurrent_connections()` slot pool (`(max_staked_connections + max_unstaked_connections) * 5/4`, default `(2000+2000)*5/4 = 5000`) [11](#0-10)  and the 2-second per-attempt window enforced by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` [12](#0-11) . Since `ClientConnectionTracker::new` counts *all* accepted-but-not-yet-verified connections against this single global counter regardless of source IP [13](#0-12) , a single attacking IP can repeatedly open (accept, immediately stall/drop, wait out or trigger the timeout, reopen) enough parallel half-open attempts to keep `open_connections` pinned near the global cap, causing subsequent legitimate `incoming.accept()` calls (staked or unstaked, any IP) to be refused via `refused_connections_too_many_open_connections` [5](#0-4) .

### Impact Explanation
This is a QoS/DoS bypass: an unprivileged, unstaked attacker can starve the leader's TPU connection-acceptance pipeline for all peers (including staked senders) by keeping the shared `open_connections`/`outstanding_incoming_connection_attempts` counters saturated with self-inflicted, permanently incomplete handshakes from one source IP, since the per-IP limiter that is supposed to bound exactly this kind of churn is a no-op against never-completing connections. This matches the "High QoS bypass" bounty category — legitimate stake-weighted TPU capacity can be denied by an unstaked party without ever registering a stake-verified connection.

### Likelihood Explanation
No stake, no privileged access, and no cluster state is required — only network reachability to the leader's TPU QUIC port. The attack only requires repeatedly opening UDP/QUIC connections and aborting before handshake completion (well within normal client capability, e.g., sending an Initial packet and then dropping the socket, or simply not responding to Handshake packets), which is trivially scriptable and fully repeatable in a loop bounded only by the attacker's own connection-creation rate and the 2-second timeout window.

### Recommendation
Apply per-IP admission control (via a lightweight, non-mutating pre-handshake counter, e.g., an atomic per-IP "outstanding attempts" map with its own cap) before `ClientConnectionTracker::new`/`incoming.accept()`, so that unverified, in-flight handshake attempts are also bounded per source IP — not just completed handshakes. Alternatively, consume a per-IP token in `ConnectionRateLimiter` optimistically at accept time (before the handshake, similar to `overall_connection_rate_limiter`) and refund it on failure/timeout, rather than only recording success in `register_connection`.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test module, using `setup_quic_server`/`make_client_endpoint` helpers already present, e.g. as used in `test_rate_limiting_establish_connection`):
1. Spawn `run_server` with default `QuicStreamerConfig`/`SwQosConfig` (or reduced `max_staked_connections`/`max_unstaked_connections` for a fast test) and a single-IP attacker (loopback).
2. From the attacker IP, in a loop, open many QUIC client endpoints/connections to the server but drop each `Connecting` future or client socket immediately after `connect()` without completing the TLS handshake (never await connection establishment) — repeat well beyond `max_connections_per_ipaddr_per_min` (e.g., 200+ attempts within a minute).
3. Assert via `stats.connection_rate_limited_per_ipaddr` that this counter stays at 0 (proving the per-IP limiter never triggers) while `stats.total_incoming_connection_attempts` grows unbounded.
4. Concurrently, from a second, legitimate staked client IP, attempt a normal connection and assert it is refused (`stats.refused_connections_too_many_open_connections` increments, or the legitimate transaction fails to arrive) while the attacker's churn keeps `stats.open_connections`/`outstanding_incoming_connection_attempts` near `qos.max_concurrent_connections()`.
5. Expected (buggy) result: legitimate staked connection is starved/refused despite the attacker having 0 stake and 0 completed handshakes and despite `max_connections_per_ipaddr_per_min` being configured low; `connection_rate_limited_per_ipaddr` remains 0 for the attacker's IP throughout.

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

**File:** streamer/src/nonblocking/quic.rs (L471-493)
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

**File:** streamer/src/quic.rs (L53-56)
```rust
/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

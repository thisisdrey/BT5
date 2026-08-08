### Title
Per-IP QUIC connection rate limiter uses non-atomic check-then-act (`is_allowed` / `register_connection`), allowing burst-limit bypass during concurrent handshakes - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` gates new QUIC connection attempts with a non-mutating `rate_limiter.is_allowed()` check before accepting, and only calls the mutating `rate_limiter.register_connection()` in `setup_connection` after the handshake completes (up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` = 2s later). Because `is_allowed()` never reserves a token, an attacker from a single IP can open far more concurrent in-flight handshakes than the configured per-IP burst (`max_connections_per_ipaddr_per_min * 10`), since all of them observe "no tokens consumed yet" simultaneously.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:359`), each incoming connection is pre-filtered with:
```rust
if !rate_limiter.is_allowed(&incoming.remote_address().ip()) { ... }
```
`ConnectionRateLimiter::is_allowed` (`streamer/src/nonblocking/connection_rate_limiter.rs:34-40`) is explicitly documented and implemented as read-only: it only checks `current_tokens`, and returns `true` if the IP has no record yet, without consuming anything.

The actual token consumption happens only in `setup_connection` (`streamer/src/nonblocking/quic.rs:483`) via `rate_limiter.register_connection(&from.ip())`, which runs **after** the QUIC handshake completes (`timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await`).

Because the check (`is_allowed`) and the act (`register_connection`) are separated by the full handshake duration (up to 2s), an attacker opening N parallel QUIC connections from one IP will have all N pass `is_allowed()` simultaneously (none has registered a token yet), and all N get spawned into `setup_connection` to perform the expensive QUIC/TLS handshake concurrently. The only backstops during this window are:
- `overall_connection_rate_limiter.current_tokens() == 0` check - itself also non-mutating/check-then-act (consumed later, in `setup_connection` line 495), so it is racy in the same way for the *global* burst limit.
- `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` - a **global, not per-IP**, concurrency cap. For `SwQos`, `max_concurrent_connections()` (`streamer/src/nonblocking/swqos.rs:518-522`) is `(max_staked_connections + max_unstaked_connections) * 5 / 4`, e.g. with defaults `(2000+2000)*1.25 = 5000`.

So a single unstaked IP, bounded only by the global cap (~5000) rather than the intended per-IP burst (default `8 * 10 = 80`), can keep up to thousands of handshakes in flight concurrently by pacing connection attempts to complete slowly-but-validly within the 2-second handshake timeout, before any of them register and get rejected by the per-IP limiter.

### Impact Explanation
This is a QoS/rate-limit evasion: the per-IP burst control (`max_connections_per_ipaddr_per_min * 10`) is not enforced atomically across concurrent attempts, letting one attacker-controlled IP consume a disproportionate share of handshake-processing CPU and of the shared `ClientConnectionTracker` global concurrency budget, which is otherwise meant to be split fairly across many distinct IPs/peers. This can starve legitimate senders' connection attempts during the burst window (CPU/handshake resource exhaustion), matching the "QoS evasion" / resource-exhaustion bounty category.

### Likelihood Explanation
Feasible for any unprivileged remote attacker: it requires only opening many parallel QUIC connections from one IP with completion times spread across the ~2s handshake timeout window so they all sample `is_allowed()` before any `register_connection()` lands. No special privileges, staking, or spoofing needed — real, valid handshakes suffice, only paced/delayed.

### Recommendation
Make the per-IP (and the global) admission check atomic with reservation: consume/reserve a token from `ConnectionRateLimiter` (and `overall_connection_rate_limiter`) at accept-time (before spawning `setup_connection`), and release/refund the token if the handshake subsequently fails or times out, instead of splitting the check and the mutation across the handshake boundary. Alternatively, track and cap the number of *outstanding* (not-yet-registered) handshake attempts per IP so pre-handshake concurrency itself cannot exceed the configured per-IP burst.

### Proof of Concept
Concurrency fuzz/integration test in `streamer/src/nonblocking/quic.rs` test module:
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_per_ip_burst_not_enforced_atomically() {
    // configure a small per-ip burst, e.g. max_connections_per_ipaddr_per_min = 2 (burst = 20)
    let SpawnTestServerResult { server_address, stats, cancel, .. } = setup_quic_server(
        None,
        QuicStreamerConfig { max_connections_per_ipaddr_per_min: 2, ..QuicStreamerConfig::default_for_tests() },
        SwQosConfig::default(),
    );

    // launch far more than burst (2*10=20) concurrent, slow-but-valid handshakes from the same IP
    let mut handles = Vec::new();
    for _ in 0..200 {
        handles.push(tokio::spawn(make_client_endpoint(&server_address, None)));
    }

    // Sample stats.outstanding_incoming_connection_attempts shortly after launch, before
    // any handshake completes and registers.
    tokio::time::sleep(Duration::from_millis(50)).await;
    let outstanding = stats.outstanding_incoming_connection_attempts.load(Ordering::Relaxed);

    // Assert violated invariant: more in-flight handshakes than the per-IP burst config allow.
    assert!(outstanding > 20, "expected per-ip burst bypass, got {outstanding}");

    for h in handles { let _ = h.await; }
    cancel.cancel();
}
```
Expected result: `outstanding` exceeds the configured per-IP burst (`max_connections_per_ipaddr_per_min * 10 = 20`), demonstrating that `is_allowed()`'s non-mutating check does not prevent more concurrent in-flight handshakes than the intended burst from a single IP. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L31-50)
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

    pub fn register_connection(&self, ip: &IpAddr) -> bool {
        if self.limiter.consume_tokens(*ip, 1).is_ok() {
            debug!("Request from IP {ip:?} allowed");
            true // Request allowed
        } else {
            debug!("Request from IP {ip:?} blocked");
            false // Request blocked
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

**File:** streamer/src/nonblocking/quic.rs (L346-399)
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

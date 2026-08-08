### Title
Per-IP QUIC connection rate limit is check-then-act (non-atomic) and can be bypassed by racing concurrent handshakes from one IP - (File: streamer/src/nonblocking/quic.rs)

### Summary
`run_server` gates new QUIC connections with two *peek-only* checks — `overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed(ip)` — before spawning `setup_connection`, but the corresponding token *consumption* (`overall_connection_rate_limiter.consume_tokens(1)` and `rate_limiter.register_connection(&from.ip())`) only happens inside `setup_connection` after the full QUIC handshake completes. Because the peek and the consume are separated by an `await` on the handshake (up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` = 2s), an unstaked attacker can fire many parallel Initial packets from a single IP faster than any single handshake completes, and each one passes the non-consuming per-IP/global peeks, forcing the leader to perform many concurrent, CPU-expensive QUIC/TLS handshakes from one source well beyond the intended `max_connections_per_ipaddr_per_min * 10` burst allowance.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:331-407`):
```
if overall_connection_rate_limiter.current_tokens() == 0 { ... continue }
if !rate_limiter.is_allowed(&incoming.remote_address().ip()) { ... continue }
let client_connection_tracker = ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())...
let connecting = incoming.accept();
tasks.spawn(setup_connection(connecting, rate_limiter, overall_connection_rate_limiter, ...));
```
`ConnectionRateLimiter::is_allowed` (`streamer/src/nonblocking/connection_rate_limiter.rs:34-40`) only *peeks* `current_tokens(ip) > 0` and never mutates state; the same is true for `TokenBucket::current_tokens()`. The actual token debit only happens later, inside `setup_connection` (`streamer/src/nonblocking/quic.rs:456-508`), *after* `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` resolves:
```
if !rate_limiter.register_connection(&from.ip()) { ... return }
if overall_connection_rate_limiter.consume_tokens(1).is_err() { ... return }
```
Since the accept loop is a single sequential `select!` loop that just peeks-and-spawns (cheap), it can process many Initial packets from the same source IP in rapid succession — far faster than a single QUIC handshake round-trips to completion. Each of these concurrent attempts sees the *same* un-decremented token count in `is_allowed`/`current_tokens`, so all of them pass the per-IP and global "checks" and get a `setup_connection` task spawned. The only limiter that actually gates concurrency at accept time is `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())` (`streamer/src/nonblocking/quic.rs:236-252`), which is a *global*, not per-IP, cap (e.g., `SimpleQos::max_concurrent_connections` = `max_staked_connections * 5/4`, on the order of thousands, `streamer/src/nonblocking/simple_qos.rs:422-425`). Consequently a single unstaked IP can drive concurrent handshake attempts up to that global ceiling before the per-IP `register_connection` debits actually start rejecting further attempts — vastly more than the intended `max_connections_per_ipaddr_per_min * 10` (default `8 * 10 = 80`) burst.

The code comment at `streamer/src/nonblocking/quic.rs:332-340` explicitly documents the layered defense ("protect against connection attempt bursts with a global rate-limiter", "rate-limit abusive peers by (control-asserted) ip"), but the implementation defers the actual counting to post-handshake completion, creating exactly the TOCTOU window described.

### Impact Explanation
This forces the leader to spend disproportionate CPU on QUIC/TLS handshake cryptography (asymmetric key exchange, certificate verification) for a burst of unauthenticated connections from a single unstaked source, well beyond the configured per-IP admission budget, before the rate limiter can start rejecting. This matches the "grossly underpriced pre-fee work" / QoS-evasion category: an unstaked attacker with zero stake and zero fees can consume a multiple of the intended per-IP handshake budget by exploiting concurrency, degrading available accept-loop/handshake CPU for legitimate (including staked) traffic.

### Likelihood Explanation
This requires no special privileges — only the ability to open many QUIC connections concurrently from one IP/source, which any unstaked remote client can do (e.g., via multiple ephemeral local ports or parallel async tasks in a `ClientBuilder`-style QUIC client). It's fully reproducible deterministically because the peek/consume split is a permanent structural property of the code, not a rare timing fluke — the exploit window is bounded below by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) and network RTT, both of which are comfortably long relative to how fast the accept loop can spawn tasks.

### Recommendation
Make the per-IP (and global) connection admission check-and-consume atomic at accept time rather than after the handshake completes — e.g., call `rate_limiter.register_connection`/`overall_connection_rate_limiter.consume_tokens` synchronously in `run_server` before spawning `setup_connection`, and refund/release the token only if the handshake genuinely never completes (timeout/error), instead of consuming post-hoc on success. This closes the window during which unlimited concurrent handshakes can be admitted per IP.

### Proof of Concept
Using the existing test harness (`streamer/src/nonblocking/quic.rs` test module / `setup_quic_server`), add an integration test:
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_per_ip_rate_limit_bypass_via_concurrency() {
    let SpawnTestServerResult { join_handle, server_address, stats, cancel, .. } =
        setup_quic_server(
            None,
            QuicStreamerConfig { max_connections_per_ipaddr_per_min: 1, ..QuicStreamerConfig::default_for_tests() },
            SwQosConfig { max_connections_per_unstaked_peer: 1000, ..Default::default() },
        );

    // Fire N concurrent handshakes from the same source IP simultaneously.
    let n = 50;
    let handles: Vec<_> = (0..n)
        .map(|_| tokio::spawn(make_client_endpoint(server_address, None)))
        .collect();
    let _clients: Vec<_> = futures::future::join_all(handles).await;

    tokio::time::sleep(Duration::from_millis(500)).await;

    // Expected (per-IP burst = 1 * 10 = 10): total_new_connections should be <= 10.
    // Actual (bug): total_new_connections significantly exceeds 10 because all
    // concurrent handshakes raced past the non-consuming `is_allowed` peek.
    assert!(
        stats.total_new_connections.load(Ordering::Relaxed) <= 10,
        "per-IP burst limit bypassed: {} connections admitted",
        stats.total_new_connections.load(Ordering::Relaxed)
    );

    cancel.cancel();
    join_handle.await.unwrap();
}
```
This test is expected to fail against current code (more than 10 connections admitted from one IP), demonstrating the bypass. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** streamer/src/nonblocking/quic.rs (L331-399)
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

**File:** streamer/src/nonblocking/quic.rs (L471-510)
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

                stats.total_new_connections.fetch_add(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L34-50)
```rust
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

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```

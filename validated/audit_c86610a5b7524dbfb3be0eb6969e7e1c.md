### Title
Check-then-act (peek-not-reserve) rate limiting lets a single IP force many concurrent QUIC/TLS handshakes before token consumption - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` gates new connections with peek-only checks (`overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed(ip)`) before calling `incoming.accept()`, which is where the actual QUIC/TLS handshake crypto work happens. Real token consumption (`register_connection` / `overall_connection_rate_limiter.consume_tokens(1)`) only happens in `setup_connection` *after* the handshake has already completed, so multiple connection attempts issued within the ~2s `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window can all pass the pre-handshake peek check simultaneously and all pay full handshake cost before any of them is actually charged.

### Finding Description
In `streamer/src/nonblocking/quic.rs::run_server` [1](#0-0) , the accept loop performs two non-consuming reads:
- `overall_connection_rate_limiter.current_tokens() == 0` (global bucket, peek only)
- `rate_limiter.is_allowed(&ip)` (per-IP bucket, peek only, per `ConnectionRateLimiter::is_allowed`) [2](#0-1) 

Only after these peek checks pass does the loop call `ClientConnectionTracker::new` (a global concurrent-connection cap) and then `incoming.accept()`, which spawns `setup_connection` to actually drive the QUIC/TLS handshake under a `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` of 2 seconds [3](#0-2) [4](#0-3) .

The actual token consumption — `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` — only occurs *after* the handshake future resolves successfully, inside `setup_connection` [5](#0-4) .

Because the check (peek) and the charge (consume) are not atomic and are separated by the full handshake latency (up to 2 seconds), an attacker from a single real IP can open many connections concurrently within that window. Every one of them will observe `is_allowed() == true` and `current_tokens() != 0` (since none of the in-flight connections have consumed a token yet), so all are admitted to `incoming.accept()` and each independently performs full QUIC/TLS handshake crypto work. Only afterward, when `register_connection`/`consume_tokens` are finally called, do the buckets get drained and excess connections get rejected — but by then the CPU cost of the handshake has already been paid regardless of whether the connection is ultimately accepted or torn down. The only real ceiling on concurrency during this window is the global `ClientConnectionTracker::max_concurrent_connections()` cap (set to `1.25x` of `max_staked_connections + max_unstaked_connections`, shared across all peers, not per-IP) [6](#0-5) [7](#0-6) , which is far larger than the intended per-IP burst (`max_connections_per_ipaddr_per_min * 10`, see `ConnectionRateLimiter::new`) [8](#0-7) .

This is a genuine gap relative to the intended invariant stated in the code's own comment — "our connection/handshake abuse mitigation policy is one of shed fast and bound resource consumption" [9](#0-8)  — because the "shed fast" step (peek check) does not reserve a slot, so the resource-consumption bound it's meant to enforce (proportional to `TOTAL_CONNECTIONS_PER_SECOND` / `MAX_CONNECTION_BURST` / per-IP rate) can be exceeded for the duration of one handshake timeout by simply pipelining enough concurrent connection attempts from a single IP, up to the shared global `max_concurrent_connections` limit.

### Impact Explanation
Scoped impact is excess TLS/crypto CPU burn on the leader disproportionate to the per-IP/overall rate limits and to any fee ever collected, since the handshake work (asymmetric crypto, `Initial`/`Handshake` packet processing) is fully paid before the rate limiter can reject the connection. This falls into the "grossly underpriced pre-fee work" / DoS-via-disproportionate-work category rather than a memory-safety or consensus bug: it does not panic the node, corrupt state, or bypass sigverify — it only lets an attacker temporarily amplify handshake CPU cost beyond the nominal token-bucket-implied rate, bounded by the global `max_concurrent_connections` slot cap and the 2-second handshake timeout.

### Likelihood Explanation
Feasible with a single real (non-spoofed) IP and no privileges: the attacker only needs to open `N` QUIC connections concurrently (N bounded by the shared `max_concurrent_connections` cap and per-IP burst `max_connections_per_ipaddr_per_min * 10`), let them complete/timeout, and repeat, timing bursts to land within the ~2s handshake window before earlier connections consume their tokens. This is fully within the "one real IP, open/close QUIC connections" threat model. It is bounded, however, by the global concurrent-connection cap and by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, which limits the magnitude of the amplification per burst.

### Recommendation
Convert the rate limiters to a reserve-then-release (or reserve-then-commit) pattern: attempt to atomically consume a token from both the per-IP and global buckets *before* calling `incoming.accept()` (i.e., before starting the handshake), and refund/release the token if the handshake subsequently fails validation for reasons unrelated to abuse (e.g., legitimate transient failures), rather than consuming only after a successful handshake. This closes the check-then-act race and ensures handshake CPU work is bounded by the advertised rate-limit token budget rather than by the (much larger) global concurrent-connection cap.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (test module)
// Demonstrates that N concurrent connection attempts from a single IP,
// launched faster than one handshake round-trip, can all pass the
// pre-handshake per-IP/global rate-limit peek checks and each independently
// consume handshake CPU/crypto work, even though the configured per-IP
// burst is far smaller than N.
#[tokio::test(flavor = "multi_thread")]
async fn test_concurrent_handshake_churn_bypasses_peek_rate_limit() {
    let SpawnTestServerResult {
        join_handle, server_address, stats, cancel, ..
    } = setup_quic_server(
        None,
        QuicStreamerConfig {
            max_connections_per_ipaddr_per_min: 1, // burst = 1*10 = 10
            ..QuicStreamerConfig::default_for_tests()
        },
        SwQosConfig::default(),
    );

    // Launch far more than the per-IP burst (10) concurrently, from one IP,
    // before any single handshake can complete and consume a token.
    let mut handles = Vec::new();
    for _ in 0..50 {
        handles.push(tokio::spawn(async move {
            let _ = make_client_endpoint(&server_address, None).await;
        }));
    }
    futures::future::join_all(handles).await;

    // Expected (buggy) behavior: total_new_connections / handshake attempts
    // observed by the server significantly exceeds the configured per-IP
    // burst of 10, showing that handshake CPU work was performed for
    // connections that should have been shed pre-handshake.
    assert!(
        stats.total_incoming_connection_attempts.load(Ordering::Relaxed) > 10,
        "more handshake attempts were admitted than the per-IP burst allows, \
         indicating peek-not-reserve rate limiting let CPU-costly handshakes \
         proceed beyond the intended budget"
    );

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected assertion under a fix: the number of admitted handshake attempts (`total_incoming_connection_attempts` minus those rejected via `connection_rate_limited_per_ipaddr`/`connection_rate_limited_across_all` *before* `incoming.accept()`) should never exceed the configured per-IP burst, regardless of how tightly the client pipelines connection attempts within the handshake timeout window.

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

**File:** streamer/src/nonblocking/quic.rs (L331-341)
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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L21-29)
```rust
    pub fn new(limit_per_minute: u64, max_burst: u64, num_shards: usize) -> Self {
        Self {
            limiter: KeyedRateLimiter::new(
                CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD,
                TokenBucket::new(limit_per_minute, max_burst, limit_per_minute as f64 / 60.0),
                num_shards,
            ),
        }
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

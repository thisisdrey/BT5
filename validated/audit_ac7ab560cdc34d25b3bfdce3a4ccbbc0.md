### Title
Per-IP and global QUIC connection rate limits are check-then-act (TOCTOU), allowing an attacker to burst far beyond `max_connections_per_ipaddr_per_min` in handshake CPU consumption - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` gates new QUIC connection attempts using non-mutating "peek" checks (`overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed()`), while the actual token consumption (`register_connection` / `consume_tokens`) only happens later in `setup_connection`, after the (CPU-expensive) TLS/QUIC handshake completes. Because the check and the mutation are not atomic and are separated by the full handshake duration, many concurrent connection attempts from the same IP (or across IPs, for the global limiter) can all pass the pre-handshake check simultaneously before any of them registers, letting an attacker force many more handshakes to complete than the configured limiter is supposed to allow.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:331-369`), each incoming connection is filtered by two checks before the handshake is even attempted:
- `overall_connection_rate_limiter.current_tokens() == 0` — a read-only peek at the global bucket [1](#0-0) 
- `rate_limiter.is_allowed(&incoming.remote_address().ip())` — a read-only peek at the per-IP bucket [2](#0-1) 

`ConnectionRateLimiter::is_allowed` explicitly does not consume tokens; its doc comment even states this is intentional because "we should only modify server state once source IP is verified": [3](#0-2) 

Only after `incoming.accept()` succeeds is `setup_connection` spawned as an independent async task per connection [4](#0-3) , and only inside `setup_connection`, after `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` completes the actual QUIC handshake, does the code call the token-consuming `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)`: [5](#0-4) 

Because `is_allowed`/`current_tokens() == 0` never mutate state, and each `setup_connection` task runs concurrently and independently, an attacker can open N parallel QUIC handshakes from a single source IP within the same `WAIT_FOR_CONNECTION_TIMEOUT` (1s) accept loop window. Every one of those N attempts observes the per-IP bucket as non-empty (since no sibling connection has called `register_connection` yet) and is admitted past the pre-handshake gate. The only mutating, immediately-enforced admission control at accept time is `ClientConnectionTracker::new`, which bounds total in-flight connections globally (not per-IP) via `qos.max_concurrent_connections()` (e.g., `(max_staked_connections + max_unstaked_connections) * 5 / 4`, which is far larger than the per-IP burst of `max_connections_per_ipaddr_per_min * 10`): [6](#0-5) [7](#0-6) 

Only after all N connections finish their handshakes does `register_connection` actually decrement the per-IP token bucket and reject/close the excess ones. The excess connections have already consumed full QUIC handshake CPU cost and occupied a `ClientConnectionTracker` slot before being rejected, meaning the intended purpose of the per-IP limiter (bounding handshake-CPU spend per source before it's incurred) is evaded by parallelism.

### Impact Explanation
This is a QoS-evasion bug: the per-IP (and global) connection admission control is designed to bound CPU/resource cost per source before expensive handshake work is performed, but the check-then-act split lets an attacker force the server to perform up to `max_concurrent_connections` full QUIC handshakes from one IP concurrently — vastly more than `max_connections_per_ipaddr_per_min` (and its 10x burst allowance) intends — before the excess connections are torn down. Repeating this every accept-loop cycle allows sustained excess handshake-CPU consumption and `ClientConnectionTracker` slot churn from a single unstaked attacker IP, degrading legitimate connection admission and CPU budget on the TPU-facing QUIC server.

### Likelihood Explanation
This requires only unprivileged network access to the leader's public TPU QUIC endpoint and the ability to open many real (non-spoofed) concurrent QUIC handshakes from one IP within roughly the 1-second `WAIT_FOR_CONNECTION_TIMEOUT` accept-loop window and the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`. No staking, keys, or special privileges are needed — just parallel socket/connection attempts, which is trivial for any client. The race window (full handshake duration) is large relative to normal single-connection processing time, making the race highly reliable in practice.

### Recommendation
Reserve/consume the per-IP and global rate-limit tokens at accept time (before spawning the handshake task) rather than after the handshake completes, refunding the token if the handshake fails or times out. Alternatively, make the check-and-reserve step atomic (e.g., a single `try_consume` with rollback) instead of a separate non-mutating peek (`is_allowed`/`current_tokens`) followed by a later mutating consume (`register_connection`/`consume_tokens`) in a different task.

### Proof of Concept
Integration test plan (in `streamer/src/nonblocking/quic.rs` test module or `tpu-client-next` integration tests):
1. Start a QUIC test server via `setup_quic_server` with `max_connections_per_ipaddr_per_min` set small (e.g., 2, giving a burst of 20 per `ConnectionRateLimiter::new`).
2. From a single loopback source IP, open `N = 200` `Connecting` futures concurrently (e.g., via `make_client_endpoint` clones bound to distinct ephemeral ports on 127.0.0.1) without awaiting completion sequentially — fire them all near-simultaneously with `futures::join_all` before any handshake completes.
3. Assert via `StreamerStats`: `stats.total_incoming_connection_attempts` and `stats.outstanding_incoming_connection_attempts` (peak value) show far more than `limit_per_minute + max_burst` (i.e., > 20) connections concurrently admitted past the pre-handshake gate and undergoing full handshake, contradicting the intended per-IP burst cap.
4. Assert that `stats.connection_rate_limited_per_ipaddr` only increments post-handshake (in `setup_connection`), confirming the excess connections were rejected only after paying the full handshake cost rather than being rejected pre-handshake.

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

**File:** streamer/src/nonblocking/quic.rs (L384-399)
```rust
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

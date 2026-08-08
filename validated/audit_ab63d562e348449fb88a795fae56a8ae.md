### Title
Per-IP and global QUIC connection rate limiters only charge on successful handshake completion, allowing unlimited unstaked handshake-attempt churn to evade `ConnectionRateLimiter`/`overall_connection_rate_limiter` - (File: streamer/src/nonblocking/quic.rs)

### Summary
In `run_server`, the per-IP (`ConnectionRateLimiter::is_allowed`) and global (`overall_connection_rate_limiter.current_tokens() == 0`) checks performed before `incoming.accept()` are non-consuming "peeks." The actual token debit (`rate_limiter.register_connection` and `overall_connection_rate_limiter.consume_tokens(1)`) only happens in `setup_connection` after a QUIC handshake **successfully completes**. An unstaked attacker who never completes the handshake (drops or stalls the connection) is therefore never charged against either limiter, letting them repeatedly trigger the handshake-initiation path for free.

### Finding Description
`run_server` gates admission with two non-mutating reads: [1](#0-0) 
Both `overall_connection_rate_limiter.current_tokens()` and `rate_limiter.is_allowed()` only inspect token counts; they do not consume tokens. The actual debits happen only in the success branch of `setup_connection`, after the QUIC/TLS handshake (`connecting.await`) resolves to `Ok(new_connection)`: [2](#0-1) 
If the handshake times out (`QUIC_CONNECTION_HANDSHAKE_TIMEOUT = 2s`) or errors, execution falls into the `Err`/timeout branches which only bump `connection_setup_timeout`/error stats — `rate_limiter.register_connection` and `overall_connection_rate_limiter.consume_tokens` are never reached: [3](#0-2) 

Consequently, an attacker who opens a connection and never completes (or deliberately drops after the initial packet) never gets registered against the per-IP token bucket (`limiter.consume_tokens` in `ConnectionRateLimiter::register_connection`) or the global `overall_connection_rate_limiter`: [4](#0-3) 

This means the per-IP bucket in `ConnectionRateLimiter::new` (seeded with `max_connections_per_ipaddr_per_min` and a 10x burst) never depletes for such traffic, so `is_allowed()` keeps returning `true` indefinitely for the attacker's IP regardless of how many handshake attempts they initiate: [5](#0-4) 

The only real bound left on this traffic is the concurrency cap enforced by `ClientConnectionTracker::new(..., qos.max_concurrent_connections())`, which limits how many handshakes can be *in flight* at once (a global concurrency cap, not per-IP), and the 2-second handshake timeout that eventually frees a slot: [6](#0-5) 

This differs from the exploit hypothesis in the question (that the token bucket refill interval itself is being outraced) — the actual root cause is that the rate limiter is never charged at all for incomplete handshakes, since consumption is gated on handshake success rather than admission/attempt.

### Impact Explanation
An unstaked attacker can indefinitely occupy up to `max_concurrent_connections` handshake slots (shared across all unstaked/staked traffic on that endpoint) by continuously opening and abandoning QUIC connections before the 2-second handshake timeout, without ever being flagged by `stats.connection_rate_limited_per_ipaddr` or `connection_rate_limited_across_all`. This forces the leader to repeatedly perform QUIC/TLS handshake-initiation crypto work and `ClientConnectionTracker` bookkeeping for each churned attempt, and denies legitimate peers (including staked ones sharing the same accept loop and endpoint) access to those concurrency slots — a QoS/rate-limit evasion that allows disproportionate, effectively free ingress-layer work relative to any fee ever paid by the attacker.

### Likelihood Explanation
Fully reachable by an unprivileged, unstaked remote client with only network access to the public TPU QUIC port; no stake, keys, or special config required. It requires only standard QUIC client behavior — open a connection and drop/stall it before the handshake completes — repeated in a loop, which is trivial to automate and fully repeatable.

### Recommendation
Charge (or provisionally reserve) a rate-limiter token at admission time (before `incoming.accept()`/handshake begins), not only on successful handshake completion, and refund it if the handshake fails, so that both the per-IP `ConnectionRateLimiter` and `overall_connection_rate_limiter` reflect connection *attempts* rather than only completed handshakes.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test module or `tpu-client-next` style tests):
1. Spin up a QUIC server via `setup_quic_server` with `max_connections_per_ipaddr_per_min` set low (e.g., 1).
2. From a single client IP, repeatedly open a raw QUIC connection (`quinn::Endpoint::connect`) and drop it immediately after the transport-level connection object is returned but before completing crypto handshake (or use a client that never responds to server's handshake flight), looping this N > 1000 times within a few seconds.
3. Assert that `stats.total_incoming_connection_attempts` grows to N while `stats.connection_rate_limited_per_ipaddr` and `stats.connection_rate_limited_across_all` remain at 0 (demonstrating the rate limiters were never charged), and that `stats.connection_setup_timeout` (or connection errors) accounts for the churned attempts.
4. Additionally assert `stats.refused_connections_too_many_open_connections` increases once concurrent attempts exceed `max_concurrent_connections`, showing the only effective throttle is the global concurrency cap, not the per-IP/overall rate limiters intended to bound this specific IP's behavior.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L270-281)
```rust
    let rate_limiter = Arc::new(ConnectionRateLimiter::new(
        quic_server_params.max_connections_per_ipaddr_per_min,
        // allow for 10x burst to make sure we can accommodate legitimate
        // bursts from container environments running multiple pods on same IP
        quic_server_params.max_connections_per_ipaddr_per_min * 10,
        num_shards,
    ));
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
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

**File:** streamer/src/nonblocking/quic.rs (L533-542)
```rust
            }
            Err(e) => {
                handle_connection_error(e, &stats, from);
            }
        }
    } else {
        stats
            .connection_setup_timeout
            .fetch_add(1, Ordering::Relaxed);
    }
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L42-50)
```rust
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

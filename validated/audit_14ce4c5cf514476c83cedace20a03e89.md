### Title
Per-IP QUIC connection rate limiter has a check-then-act race allowing burst evasion of `max_connections_per_ipaddr_per_min` - ([File: streamer/src/nonblocking/quic.rs])

### Finding Description
The per-IP connection admission control is split across two phases that are not atomic with each other:

1. In the accept loop (`run_server`), before the handshake even starts, the code only *peeks* at remaining tokens via `rate_limiter.is_allowed(&incoming.remote_address().ip())`, which does not consume any tokens: [1](#0-0) 
2. The token is only actually consumed later, in `setup_connection`, after the QUIC handshake completes (bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT = 2s`), via `rate_limiter.register_connection(&from.ip())`: [2](#0-1) 

`ConnectionRateLimiter::is_allowed` explicitly documents this "check-only" semantics ("just checking should not mutate state") while `register_connection` is the only mutating call: [3](#0-2) 

Because `is_allowed` is a non-consuming peek and `register_connection` only fires after the (up to 2-second) handshake completes, an unstaked attacker can open many parallel QUIC connection attempts from the same source IP within that window. Each attempt independently observes `current_tokens(ip) > 0` (since none of the concurrent attempts have decremented the bucket yet) and is admitted past the per-IP check in the accept loop. Only after each handshake finishes does `register_connection` retroactively decrement the bucket, by which time all of them have already consumed a `ClientConnectionTracker` slot and completed the handshake. This is the same TOCTOU pattern the accompanying comment in `run_server` claims to guard against ("cap total connections per-peer/ip"), but the guard is only advisory pre-handshake and enforced post-handshake, leaving a race window sized by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.

Note the same pattern also exists for `overall_connection_rate_limiter` (`current_tokens() == 0` peek pre-accept vs. `consume_tokens(1)` post-handshake).

### Impact Explanation
This is a QoS evasion of the per-IP connection admission rate limiter: an attacker can complete more handshakes from one IP within a short burst window than `max_connections_per_ipaddr_per_min` is intended to allow per unit time, before the accounting catches up. The practical blast radius is bounded, however, by two independently-enforced, mutex-guarded hard caps that are unaffected by this race: the global `ClientConnectionTracker` cap (`qos.max_concurrent_connections()`) taken before accept, [4](#0-3) , and the per-peer concurrent-connection cap in `ConnectionTable` (`max_connections_per_unstaked_peer`, default 8), enforced under a `tokio::sync::Mutex` at connection-add time in `SwQos::try_add_connection` [5](#0-4) . So the vulnerability degrades the intended "spread connections over a minute" throttle into a burst-capped-by-other-limits behavior rather than an unbounded resource exhaustion.

### Likelihood Explanation
Feasible for any unstaked remote attacker with no special privileges: it only requires opening several QUIC connections to the public TPU port faster than the ~2-second handshake timeout from a single source IP, which is trivial to script. Repeatable on demand, bounded per burst by other connection caps.

### Recommendation
Reserve/consume a token from the per-IP (and overall) rate limiter atomically at accept time (before spawning `setup_connection`), and refund it if the handshake fails/times out, instead of deferring the actual `consume_tokens` call to after handshake completion. This closes the TOCTOU window between `is_allowed` and `register_connection`.

### Proof of Concept
Integration test in `streamer/src/nonblocking/quic.rs` test module (or a new integration test):
1. Configure `QuicStreamerConfig { max_connections_per_ipaddr_per_min: N, .. }` with small `N` (e.g., 3) and a permissive `SwQosConfig` (`max_connections_per_unstaked_peer` large, e.g. 100) so the per-peer cap doesn't mask the race.
2. From a single source IP, spawn `2*N` parallel QUIC client handshakes (via `make_client_endpoint`) simultaneously, all initiated within well under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s).
3. Wait for all handshakes to resolve (accepted or closed by server).
4. Assert: number of connections that fully complete the handshake and are added to the connection table exceeds `N` (the configured per-minute burst-equivalent), demonstrating the rate limiter allowed more concurrent completions than intended, while `stats.connection_rate_limited_per_ipaddr` only increments post-hoc for the excess rather than preventing them from ever attempting/completing the handshake.

### Citations

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

**File:** streamer/src/nonblocking/swqos.rs (L415-437)
```rust
                ConnectionPeerType::Unstaked => {
                    if let Ok((last_update, cancel_connection, stream_counter)) = self
                        .prune_unstaked_connections_and_add_new_connection(
                            client_connection_tracker,
                            connection,
                            self.unstaked_connection_table.clone(),
                            self.config.max_unstaked_connections,
                            conn_context,
                        )
                        .await
                    {
                        self.stats
                            .connection_added_from_unstaked_peer
                            .fetch_add(1, Ordering::Relaxed);
                        conn_context.in_staked_table = false;
                        conn_context.last_update = last_update;
                        conn_context.stream_counter = Some(stream_counter);
                        return Some(cancel_connection);
                    } else {
                        self.stats
                            .connection_add_failed_unstaked_node
                            .fetch_add(1, Ordering::Relaxed);
                    }
```

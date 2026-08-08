### Title
Per-IP QUIC connection rate limit can be bypassed via concurrent handshakes (TOCTOU between `is_allowed` and `register_connection`) - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The per-IP QUIC connection rate limiter is enforced with a check-then-act pattern split across two different points in time: a non-mutating peek (`ConnectionRateLimiter::is_allowed`) in the synchronous accept loop of `run_server`, and the actual token consumption (`ConnectionRateLimiter::register_connection`) inside the asynchronously spawned `setup_connection` task, which only runs after the QUIC handshake completes. Because many `setup_connection` tasks can be in flight concurrently for the same source IP before any of them reaches `register_connection`, an attacker can open several connections back-to-back and have all of them pass the `is_allowed` peek before any tokens are actually consumed.

### Finding Description
In `run_server` (streamer/src/nonblocking/quic.rs), for every accepted `Incoming` the loop calls: [1](#0-0) 

`ConnectionRateLimiter::is_allowed` only peeks `current_tokens(ip) > 0` — it does not consume anything: [2](#0-1) 

The actual token consumption happens later, inside `setup_connection`, only *after* the QUIC handshake (`connecting.await`, bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` = 2s) has completed: [3](#0-2) 

Between the `is_allowed` peek in the main accept loop and the eventual `register_connection` consume in the spawned task, the token bucket for that IP is unchanged. Since `run_server`'s accept loop processes one `Incoming` per iteration but spawns `setup_connection` as an independent task via `tasks.spawn(...)` (streamer/src/nonblocking/quic.rs:389-399) rather than awaiting it, an attacker opening several connections from the same source IP within the ~2 second handshake window will have each of those connections' `is_allowed` peek see the same (not-yet-decremented) token count and pass. Each spawned task then independently calls `register_connection`, each consuming one token — so N parallel handshakes can consume N tokens even though the pre-check gate was only evaluated against the pre-handshake bucket state. This defeats the purpose of doing the cheap `is_allowed` pre-check before spending handshake resources, allowing an attacker to force the server to complete the full QUIC handshake for a burst of connections beyond `max_connections_per_ipaddr_per_min` (already 10x'd for burst tolerance at line 274) by parallelizing connection attempts instead of serializing them.

### Impact Explanation
This is a QoS evasion for the per-IP connection rate limiter: an unstaked attacker from a single IP can force the server to spend handshake CPU/crypto work and consume connection-table/QoS resources for more concurrent connections than `max_connections_per_ipaddr_per_min` (and its 10x burst allowance) intend to permit within the handshake window, by simply issuing connection attempts in parallel rather than serially. This matches the "per-IP QoS bypass allowing burst beyond configured `max_connections_per_ipaddr_per_min`" impact category. The bypass is bounded (not unlimited) by the global `overall_connection_rate_limiter` (`MAX_CONNECTION_BURST` = 1000, `TOTAL_CONNECTIONS_PER_SECOND` = 2500.0) and by `ClientConnectionTracker`'s `max_concurrent_connections()` cap, so it does not lead to unbounded memory or a full DoS, but it does let a single IP evade the intended per-IP throttle for the duration of a handshake window.

### Likelihood Explanation
Feasible and repeatable: an attacker needs only to open multiple QUIC connections back-to-back from the same source IP (no special privileges, staking, or spoofing required) so that their handshakes race concurrently within the `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) window before any of the spawned `setup_connection` tasks call `register_connection`. This can be triggered on every fresh burst against the leader's public TPU QUIC port and is trivially reproducible with `make_client_endpoint`-style parallel connections.

### Recommendation
Reserve a token at the point of the pre-handshake check rather than merely peeking, e.g. have the accept-loop call `register_connection` (or an equivalent atomic reserve) before accepting the handshake, and release/refund the token if the handshake later fails/times out, or alternatively require the peek and the consume to happen atomically inside a single call guarded per-IP so concurrent parallel handshakes cannot all observe the same pre-decrement state.

### Proof of Concept
Integration test plan (extending existing `streamer/src/nonblocking/quic.rs` / `connection_rate_limiter.rs` test harness or `tpu-client-next` integration tests):
```rust
// Configure setup_quic_server with max_connections_per_ipaddr_per_min = 1 (burst = 10)
// Spawn N (e.g. 15) make_client_endpoint connections concurrently (tokio::join!/FuturesUnordered)
// from the same source IP, all launched before any prior connection's handshake could
// plausibly complete registration.
// Assert: the number of connections whose handshake actually succeeds (not closed with
// CONNECTION_CLOSE_CODE_DISALLOWED / CONNECTION_CLOSE_REASON_DISALLOWED) exceeds the
// configured burst limit (max_connections_per_ipaddr_per_min * 10), demonstrating that
// `is_allowed` + `register_connection` do not serialize enforcement across concurrent
// setup_connection tasks.
```

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

### Title
Per-IP QUIC connection rate limiting can be bypassed by unstaked attackers who churn handshake-incomplete connections, exhausting the global `max_concurrent_connections` budget - (File: streamer/src/nonblocking/quic.rs)

### Finding Description
In `run_server` (streamer/src/nonblocking/quic.rs), the per-IP check `rate_limiter.is_allowed(&incoming.remote_address().ip())` at [1](#0-0)  is a **non-consuming** read of `KeyedRateLimiter::current_tokens` [2](#0-1) . The only call that actually *consumes* a token, `rate_limiter.register_connection(&ip)`, happens inside `setup_connection` **only after the QUIC handshake completes successfully** (`Ok(new_connection)` branch) [3](#0-2) .

Between those two points, `ClientConnectionTracker::new` reserves a slot out of the **global** (not per-IP) `qos.max_concurrent_connections()` budget and increments `stats.open_connections` before the handshake is attempted [4](#0-3) , and the slot is held for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) while `setup_connection` awaits the QUIC handshake future via `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` [5](#0-4) [6](#0-5) .

Because `is_allowed` never decrements the bucket, an attacker's source IP that has never completed a handshake will always pass the per-IP gate (an IP with no history returns `true` unconditionally, per the `None => true` branch) [2](#0-1) . Likewise, `overall_connection_rate_limiter.current_tokens() == 0` at line 347 is only a non-consuming global gate; the actual consumption `overall_connection_rate_limiter.consume_tokens(1)` also happens post-handshake [7](#0-6) . So an attacker that never completes the TLS handshake (e.g., sends only `Initial`/`ClientHello` and stalls) never trips either rate limiter, yet still consumes a `ClientConnectionTracker` slot for up to 2 seconds per attempt.

By opening connection attempts from a single IP faster than the 2-second handshake timeout expires (limited only by local socket/file-descriptor and CPU cost of driving many `Connecting` futures), an attacker can keep a large fraction of `max_concurrent_connections` occupied, causing `ClientConnectionTracker::new` to return `Err(())` for subsequent legitimate connections (staked or unstaked), which increments `refused_connections_too_many_open_connections` and drops the connection via `incoming.refuse()` [8](#0-7) .

### Impact Explanation
This is a denial-of-service on TPU ingress: an unstaked attacker from a single source IP can consume the shared `max_concurrent_connections` budget (a resource shared across all staked/unstaked senders) without ever completing a handshake and thus without ever being penalized by the per-IP or global QUIC connection rate limiters. Legitimate unstaked (and potentially staked, depending on how `max_concurrent_connections` is derived from staked/unstaked connection limits) senders attempting genuine handshakes are refused via `refused_connections_too_many_open_connections`, blocking transaction ingress. This matches the "unstaked/staked connection ... limits enforced per source" invariant described in the question and falls under a QoS-evasion / DoS bounty category rather than block-invalidity.

### Likelihood Explanation
Feasibility is straightforward for an unprivileged remote attacker: they need only open QUIC connections and avoid completing the handshake (e.g., drop the connection after sending `Initial`, or use a client that stalls mid-handshake), repeated fast enough to refill their occupied slot count within the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window. No stake, keys, or special network position are required — only the ability to reach the TPU QUIC port and send raw UDP/QUIC `Initial` packets, well within the stated threat model.

### Recommendation
Perform per-IP (and/or per-subnet) admission control **before** allocating a `ClientConnectionTracker` slot and starting the handshake, not only after handshake completion. Concretely:
- Track "in-flight, unconfirmed" connection attempts per IP (separately from the confirmed-connection token bucket) and cap them, so a single IP cannot occupy an unbounded share of `max_concurrent_connections` while unverified.
- Alternatively, consume a (refundable) token from the per-IP rate limiter at accept time and refund it if the handshake fails/times out, so repeated non-completing attempts from one IP are throttled immediately instead of only being throttled after the fact.
- Consider reserving a per-IP or per-subnet cap on `outstanding_incoming_connection_attempts`, independent of the global `max_concurrent_connections`.

### Proof of Concept
Integration test plan (extending existing patterns in `tpu-client-next/tests/connection_workers_scheduler_test.rs` and `streamer/src/nonblocking/quic.rs` test utilities):
1. Start a QUIC server via `setup_quic_server` with a small `max_concurrent_connections` (via `SwQosConfig`/`SimpleQosConfig`) and default `max_connections_per_ipaddr_per_min`.
2. From a single source IP, spawn N > `max_concurrent_connections` client endpoints that send only the QUIC `Initial` packet (e.g., open the UDP socket and send a raw `ClientHello`-triggering packet, or use a quinn client that is paused before completing the handshake) without completing the TLS handshake, issuing them within less than `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) of each other.
3. Concurrently, attempt a normal, fully-handshaking connection from a *different* legitimate client.
4. Assert:
   - `stats.outstanding_incoming_connection_attempts` reaches close to `max_concurrent_connections` even though `stats.connection_rate_limited_per_ipaddr` remains 0 (proving the per-IP limiter never engaged).
   - The legitimate client's connection attempt is refused, and `stats.refused_connections_too_many_open_connections` increments, demonstrating denial of service to a legitimate sender caused solely by one attacking IP's handshake churn.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L80-80)
```rust
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
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

**File:** streamer/src/nonblocking/quic.rs (L371-383)
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
```

**File:** streamer/src/nonblocking/quic.rs (L471-475)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/quic.rs (L476-493)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L495-508)
```rust
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

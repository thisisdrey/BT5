### Title
Single unstaked IP can starve the global `max_concurrent_connections` slot pool with slow/partial QUIC handshakes - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` gates a new `Incoming` on `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())`, a single **global** (not per-IP) semaphore, before the QUIC handshake is verified [1](#0-0) . The per-IP and overall rate limiters checked at this stage only *read* current token counts (`current_tokens`/`is_allowed`) and are never *consumed* until after a handshake actually completes in `setup_connection` [2](#0-1) [3](#0-2) . This lets a single real (non-spoofed) attacker IP, using many ephemeral source ports, open connections that stall the handshake and consume `max_concurrent_connections` global slots for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) each, without ever tripping its own per-IP rate limit.

### Finding Description
In `run_server`, for every accepted `Incoming`:
1. The overall rate limiter is checked with `overall_connection_rate_limiter.current_tokens() == 0` — a read-only check, no `consume_tokens` call happens here [4](#0-3) .
2. The per-IP rate limiter is checked with `rate_limiter.is_allowed(&ip)` — also read-only, per `ConnectionRateLimiter::is_allowed`, which returns `true` for any IP it has not yet "registered" a completed connection for [5](#0-4) .
3. Only `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())` actually consumes a bounded, shared resource at this stage — it increments `stats.open_connections` and errors only when the process-wide `open_connections >= max_concurrent_connections` [6](#0-5) .
4. The connection is then accepted (`incoming.accept()`) and handed to `setup_connection`, which awaits the handshake under a `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` = 2 seconds [7](#0-6) , `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` being defined as `Duration::from_secs(2)` [8](#0-7) .
5. `rate_limiter.register_connection(&from.ip())` (the actual token-consuming call) and `overall_connection_rate_limiter.consume_tokens(1)` only execute **after** the handshake succeeds (`Ok(new_connection)` branch) [3](#0-2) .

Consequence: an attacker who completes just enough of the QUIC handshake to be accepted by `incoming.accept()` but then withholds the final handshake step (dropping/never-sending the client Finished/Handshake completion) never triggers either rate limiter's consumption path. The attacker only needs to occupy the global `open_connections` counter (protected solely by `ClientConnectionTracker`) up to `max_concurrent_connections`, hold it for ~2 seconds (until `connection_setup_timeout` fires), then immediately repeat from new ephemeral source ports (same IP, no rate-limit penalty accrued) to refill the slot pool. Because the tracker limit is global rather than per-IP, one attacking IP alone can occupy the entire slot pool, causing legitimate concurrent connection attempts (staked or unstaked) to hit `ClientConnectionTracker::new`'s `Err(())` branch and increment `refused_connections_too_many_open_connections`, then be refused via `incoming.refuse()` [9](#0-8) .

The design comment in the code acknowledges the trade-off ("shed fast and bound resource consumption... limit duration of in-flight connection attempts with a timeout... protect against connection attempt bursts with a global rate-limiter... rate-limit abusive peers by (control-asserted) ip") [10](#0-9) , but the implementation of "cap total connections per-peer/ip" for in-flight (unverified) connections is missing: `ClientConnectionTracker`'s cap is global, not per-IP, so the per-IP protection only ever engages after a connection successfully completes the handshake.

### Impact Explanation
This is a denial-of-service against the QUIC TPU ingress path reachable by any unstaked/unprivileged remote client: a single attacking IP address can exhaust the entire node's global handshake/connection-slot budget (`max_concurrent_connections`), causing legitimate senders' `Incoming` connections to be refused (`refused_connections_too_many_open_connections`) repeatedly, in cycles bounded by the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`. This matches the "temporary denial of service to validator/RPC ingress" bounty category — it does not require staked/leader control, config changes, or multiple cooperating clients (a single IP with many source ports suffices), and it is not excluded by the SECURITY.md carve-outs (it isn't a dependency/sBPF/metrics/Geyser/Alpenglow/snapshot/bootstrap/RPC-getProgramAccounts issue).

### Likelihood Explanation
Feasibility is high: opening many concurrent UDP/QUIC connections from one IP using distinct source ports, and stalling the handshake (e.g., never returning the final handshake flight, or simply not responding after the Initial exchange) is straightforward with standard QUIC client libraries, and requires no stake, no gossip presence, and no spoofing (the attacker needs bidirectional visibility to actually stall selectively at the right handshake stage, though even a naive "connect and go silent" client on many ports achieves the same effect via `connection_setup_timeout`). It is fully repeatable — nothing in the code path penalizes the attacker's IP for these incomplete attempts, so the attack can be sustained indefinitely by continuously refilling the slot pool every ~2 seconds.

### Recommendation
Apply a per-IP admission cap for the pre-handshake ("in-flight") stage, not just post-handshake. Concretely, before or when creating `ClientConnectionTracker`, additionally track and bound `outstanding_incoming_connection_attempts`-per-IP (a lightweight counter keyed by IP, symmetric to `stats.outstanding_incoming_connection_attempts`), and refuse new attempts from an IP that already holds an unreasonable share of in-flight handshake slots. Alternatively, make the per-IP `ConnectionRateLimiter` consume a token at accept time (before handshake completion) rather than only on successful handshake, with a compensating refund/no-op if the connection is legitimately established, so that repeated incomplete handshake attempts from one IP are throttled the same way completed connection bursts are.

### Proof of Concept
Integration test plan (extending the existing `streamer::nonblocking::quic` test module, which already has `setup_quic_server`/`make_client_endpoint` helpers):
1. Start a QUIC server via `setup_quic_server(...)` with a small `max_concurrent_connections` (e.g., via `SwQosConfig`/test QoS config) to make the ceiling reachable quickly, e.g. 8.
2. From one fixed loopback/local IP, spawn N (>= `max_concurrent_connections`) client `Endpoint::connect` attempts to the server that intentionally stall the handshake (e.g., create the QUIC client connection but drop it / avoid completing 1-RTT keys, or use a raw UDP socket that sends only the Initial packet and then goes silent) — enough to occupy every `ClientConnectionTracker` slot for the ~2s `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window.
3. Concurrently, attempt one legitimate `make_client_endpoint(&server_address, None).await` connection from a different source port (same test IP) and assert it fails to connect within the handshake window.
4. Assert `stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) >= 1` for the legitimate attempt, while `stats.connection_rate_limited_per_ipaddr` and `stats.connection_rate_limited_across_all` remain at 0 for the attacker's stalled attempts (demonstrating the rate limiters never engaged).
5. Repeat the stall cycle after the 2s timeout elapses and show `refused_connections_too_many_open_connections` continues to increment, demonstrating a sustained, repeatable denial of handshake slots.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L78-80)
```rust
/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
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

**File:** streamer/src/nonblocking/quic.rs (L332-340)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L371-384)
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
```

**File:** streamer/src/nonblocking/quic.rs (L470-476)
```rust
{
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
```

**File:** streamer/src/nonblocking/quic.rs (L483-508)
```rust
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

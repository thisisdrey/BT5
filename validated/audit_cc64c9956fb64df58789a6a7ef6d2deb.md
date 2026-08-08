### Title
Per-IP QUIC connection rate limiter is bypassed by handshake-stalling connections, allowing sustained exhaustion of the global concurrent-connection budget - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The per-IP `ConnectionRateLimiter` only consumes tokens in `register_connection`, which is called from `setup_connection` after a QUIC handshake **successfully completes**. An attacker that opens QUIC `Initial` packets and stalls before finishing the handshake never triggers `register_connection`, so `is_allowed` (which merely checks whether tokens already exist and are non-zero, defaulting to `true` for any IP with no prior record) never throttles them. This lets an attacker repeatedly acquire `ClientConnectionTracker` slots (bounded only by the 2s `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` per slot and the global rate limiter), keeping `open_connections` pinned near `qos.max_concurrent_connections()` and causing legitimate connections to be refused via `refused_connections_too_many_open_connections`.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:331-379`), the per-IP check before accepting a connection is `rate_limiter.is_allowed(&incoming.remote_address().ip())` [1](#0-0) . `ConnectionRateLimiter::is_allowed` only reads `current_tokens`; if the IP has no record it returns `true` without consuming anything [2](#0-1) . The only place tokens are actually consumed is `register_connection`, and this is called exclusively in `setup_connection` **after** the QUIC handshake future resolves successfully within `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` [3](#0-2) , [4](#0-3) .

Meanwhile, `ClientConnectionTracker::new` increments the global `open_connections` counter and checks it against `qos.max_concurrent_connections()` *before* the handshake completes, right after passing the (non-consuming) per-IP check [5](#0-4) , [6](#0-5) . If the handshake never completes (attacker stalls just under the 2s timeout), the slot is held for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` and then released only when `setup_connection` times out and the `ClientConnectionTracker` is dropped [7](#0-6) , [8](#0-7) .

Since `register_connection` is never invoked for a stalling attacker, the per-IP token bucket is never drained for that IP, so `DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE` provides no protection against this pattern — the attacker can keep opening new `Initial`-triggering incoming connections from the same IP (or rotated IPs) indefinitely. The only remaining backstop is the global `overall_connection_rate_limiter` (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, burst `MAX_CONNECTION_BURST = 1000`) [9](#0-8) , checked only via `current_tokens() == 0` (non-consuming) at this pre-handshake stage [10](#0-9) , so it does not meaningfully limit the *rate* of `ClientConnectionTracker::new` acquisitions either.

With defaults `DEFAULT_MAX_STAKED_CONNECTIONS = 2000` and `DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000` [11](#0-10) , `max_concurrent_connections()` in `SwQos` computes `(max_staked + max_unstaked) * 5 / 4 = 5000` [12](#0-11) . To sustain occupancy near this cap with each slot bounded to 2 seconds, an attacker needs roughly 2500 new stalled connection attempts per second — comfortably within reach of the global token bucket's sustained rate, and the per-IP mitigation does nothing to stop it since it never fires for stalled connections.

### Impact Explanation
This is a QoS-evasion / resource-exhaustion issue: an unprivileged remote attacker can keep the leader's global `open_connections` counter pinned near `max_concurrent_connections()` by continuously opening and abandoning QUIC handshakes just under the 2-second timeout, causing `ClientConnectionTracker::new` to fail for legitimate incoming connections and increment `refused_connections_too_many_open_connections`. This degrades TPU/TPU-forward connection availability for legitimate stakers/clients without requiring any staked identity, matching the "QoS evasion" / availability-degradation bounty category (a DoS-style connection-starvation issue against the QUIC ingress path).

### Likelihood Explanation
Feasible with unprivileged access: the attacker only needs to open QUIC `Initial` packets toward the leader's public TPU port and avoid completing the handshake (or complete it just past the point where they can stop responding) before `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) elapses. No staking, gossip, or protocol-privileged capability is required. The needed connection-attempt rate (~2500/s to fill a 5000-slot default budget) is within the explicit design headroom of the global rate limiter (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, burst `1000`), and the per-IP limiter is structurally incapable of stopping it because it only consumes tokens on handshake *success*. This is repeatable indefinitely as long as the attacker sustains the stalling pattern.

### Recommendation
Consume (or reserve) per-IP and/or global rate-limiter tokens at the point `ClientConnectionTracker::new` succeeds (i.e., before/at slot acquisition) rather than only after a successful handshake, so that incomplete/abandoned handshake attempts are also charged against the attacker's budget. Alternatively, track and rate-limit slot acquisitions themselves (e.g., a per-IP cap on outstanding/unconfirmed `ClientConnectionTracker` instances, refunding on success) so a single IP or small set of IPs cannot monopolize a large fraction of `max_concurrent_connections()` regardless of handshake completion.

### Proof of Concept
Integration test sketch (extending existing `streamer/src/nonblocking/quic.rs` test harness, using `setup_quic_server`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_handshake_stalling_exhausts_open_connection_budget() {
    // small max_concurrent_connections for a fast test, e.g. SwQosConfig with
    // max_staked_connections = 4, max_unstaked_connections = 4 => cap = 10
    let SpawnTestServerResult { server_address, stats, cancel, join_handle, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), small_swqos_config());

    // Spawn attacker "connections" that create UDP sockets and send only an
    // Initial-like packet (or use quinn Endpoint::connect but never drive the
    // future to completion / drop it just before QUIC_CONNECTION_HANDSHAKE_TIMEOUT),
    // looping continuously from few source ports/IPs to stay under is_allowed()'s
    // "no record yet" bypass.
    let attacker_task = tokio::spawn(async move {
        loop {
            // open N stalled connection attempts, sleep ~1.9s, drop, repeat
        }
    });

    tokio::time::sleep(Duration::from_secs(5)).await;

    // A legitimate client attempts to connect and complete the handshake.
    let legit = make_client_endpoint(&server_address, None).await;
    // assert legit handshake fails / server stats show refusal:
    assert!(
        stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0,
        "legitimate connections should not be starved by stalled attacker connections"
    );

    attacker_task.abort();
    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected (pre-fix) result: `refused_connections_too_many_open_connections` increases while the per-IP `connection_rate_limited_per_ipaddr` stat stays at 0 for the attacker traffic, confirming the per-IP limiter never engaged despite sustained abusive connection churn.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-76)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;
```

**File:** streamer/src/nonblocking/quic.rs (L80-80)
```rust
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

**File:** streamer/src/quic.rs (L46-48)
```rust
pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

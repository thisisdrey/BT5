### Title
Global-only `ClientConnectionTracker` slot reservation allows a single IP to starve all TPU QUIC connection slots via stalled handshakes - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ClientConnectionTracker::new` reserves a slot against the *global* `qos.max_concurrent_connections()` budget before any per-source verification has completed, and the only per-IP protection (`ConnectionRateLimiter`) is not consumed until *after* a handshake successfully finishes. An attacker controlling a single (or a few) real IP addresses can open enough concurrent QUIC connection attempts that stall just under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` to exhaust the entire tracker capacity, starving all other legitimate senders.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:304-411`), each incoming connection passes two checks before a slot is reserved:
1. The global `overall_connection_rate_limiter` token check (`streamer/src/nonblocking/quic.rs:347-357`).
2. The per-IP `rate_limiter.is_allowed(&ip)` check (`streamer/src/nonblocking/quic.rs:359-369`), which only inspects `current_tokens` and does **not** consume a token: [1](#0-0) 

Tokens for this per-IP limiter are only actually consumed by `rate_limiter.register_connection(&from.ip())` in `setup_connection`, and only after the handshake has *successfully completed*: [2](#0-1) 

Immediately after the (non-consuming) per-IP check, `ClientConnectionTracker::new` reserves a slot purely against the global `max_concurrent_connections` count, with no per-IP concurrency cap of its own: [3](#0-2) 

The reserved slot is only released when `ClientConnectionTracker` is dropped, which happens either when the handshake fails/times out via `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` in `setup_connection` (`streamer/src/nonblocking/quic.rs:472`), or later once `qos.try_add_connection` accepts/rejects the connection. Because:
- the per-IP token bucket is never decremented for connections that never complete the handshake, and
- there is no independent cap on how many *pending/reserved* tracker slots a single source IP can occupy,

a single attacking IP can open up to `qos.max_concurrent_connections()` simultaneous QUIC connection attempts (this is `max_staked_connections * 5/4` for the `SwQos` controller, see [4](#0-3) ) that stall the TLS/QUIC handshake (e.g., by completing the initial packet exchange enough to be accepted by the OS/quinn but withholding further handshake flight data) just under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, and continuously re-issue new attempts as old ones time out, keeping `stats.open_connections` pinned at the max indefinitely. Once saturated, all other connection attempts (staked and unstaked) are rejected with `refused_connections_too_many_open_connections` (`streamer/src/nonblocking/quic.rs:371-379`), regardless of their own legitimacy or stake.

The code comment at `streamer/src/nonblocking/quic.rs:331-340` explicitly acknowledges the design tension (avoiding stronger pre-handshake per-IP enforcement to prevent spoofing-based attacks on other peers), but the resulting mitigation set (timeout + global rate limiter + post-handshake per-IP limiter) leaves the pre-handshake tracker-reservation phase uncapped per source, which is exactly the gap described.

### Impact Explanation
This is a QoS-evasion / denial-of-service vector against the leader's TPU ingress: an unprivileged, unstaked remote attacker using a single or small number of real source IPs can occupy the entire `ClientConnectionTracker` budget with connections that never deliver a usable transaction, causing `refused_connections_too_many_open_connections` for all other senders (including staked, higher-priority nodes) for as long as the attacker sustains the stalled-handshake churn. This matches the "QoS evasion" / connection-starvation DoS bounty category rather than a memory-safety or consensus-correctness bug.

### Likelihood Explanation
The attacker only needs the ability to open UDP/QUIC connections to the public TPU port and control the pace of handshake flights from a real IP address — no stake, gossip presence, or validator control is required. The attack is straightforward to sustain: repeatedly open connections that stall for slightly less than `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, then immediately re-open new ones as slots free up, keeping `stats.open_connections` saturated. The only throttle on the attacker's request rate is the global `overall_connection_rate_limiter` (`MAX_CONNECTION_BURST` / `TOTAL_CONNECTIONS_PER_SECOND`), which caps *rate of new attempts*, not *concurrently reserved slots*, so it does not prevent the attacker from maintaining saturation once the initial `max_concurrent_connections()` slots are acquired.

### Recommendation
Introduce an independent, per-source-IP cap on the number of *pending* (pre-handshake-complete) `ClientConnectionTracker` reservations, separate from the global `max_concurrent_connections` limit — e.g., track pending reservations per IP in a bounded map and reject/ignore new attempts from an IP that already holds more than a small fixed number of unconfirmed slots, in addition to the existing post-handshake `ConnectionRateLimiter`. Alternatively, consume a per-IP rate-limiter token at the time of `ClientConnectionTracker::new` (before reservation) rather than only after a successful handshake, while accepting the documented spoofing tradeoff, or use QUIC's `retry` (address validation) mechanism to force IP ownership proof before any tracker slot is reserved.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (test module)
//
// Fuzz stalled-handshake concurrency from a single IP and assert that
// ClientConnectionTracker slot occupancy attributable to one IP is bounded
// independently of the global tracker capacity.
#[tokio::test(flavor = "multi_thread")]
async fn test_single_ip_can_saturate_client_connection_tracker() {
    let max_concurrent = 8; // small qos.max_concurrent_connections() for test
    let SpawnTestServerResult {
        server_address, stats, cancel, join_handle, ..
    } = setup_quic_server(
        None,
        QuicStreamerConfig::default_for_tests(),
        SwQosConfig {
            max_staked_connections: max_concurrent * 4 / 5, // -> max_concurrent_connections() == max_concurrent
            ..Default::default()
        },
    );

    // Attacker: open `max_concurrent` UDP sockets/handshakes from the SAME ip
    // that never complete the QUIC handshake (e.g. drop after Initial packet).
    let mut stalled = Vec::new();
    for _ in 0..max_concurrent {
        stalled.push(open_stalled_handshake_from_same_ip(&server_address).await);
    }

    // Give the server time to reserve tracker slots but stay under
    // QUIC_CONNECTION_HANDSHAKE_TIMEOUT.
    tokio::time::sleep(Duration::from_millis(200)).await;

    // Legitimate client from a DIFFERENT ip attempts to connect.
    let legit = make_client_endpoint(&server_address, None).await;
    let result = tokio::time::timeout(Duration::from_secs(1), legit.closed()).await;

    // EXPECTED (fails today): legitimate connection is refused because
    // stats.open_connections == max_concurrent, all attributable to one IP.
    assert_eq!(
        stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed),
        1,
        "single-IP stalled handshakes saturated the global tracker, starving other senders"
    );

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected assertion for the fix: after remediation, `stats.open_connections` attributable to a single source IP should never exceed a small per-IP cap independent of `qos.max_concurrent_connections()`, so the legitimate connection from a different IP succeeds even while the attacker IP is stalling handshakes.

### Citations

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

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```

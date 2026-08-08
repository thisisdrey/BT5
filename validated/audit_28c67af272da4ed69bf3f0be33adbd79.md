## Analysis Result



### Title
Single-IP handshake-stalling flood exhausts the shared `ClientConnectionTracker` pool, bypassing per-IP rate limiting - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The per-IP `ConnectionRateLimiter::is_allowed` check only rejects IPs that have already completed a handshake and called `register_connection`; an IP that never finishes its QUIC handshake is never "registered" and therefore is never rate-limited. Combined with the fact that `overall_connection_rate_limiter` tokens are also only consumed after a successful handshake, the only real cap on unverified, in-flight connection attempts is the shared `ClientConnectionTracker` capacity (`qos.max_concurrent_connections()`), which a single attacking IP can monopolize by continuously opening and stalling connections just short of `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.

### Finding Description
In `run_server` [1](#0-0) , an incoming connection is gated by three checks before a handshake even begins: `overall_connection_rate_limiter.current_tokens() == 0` (a non-consuming peek), `rate_limiter.is_allowed(&ip)`, and `ClientConnectionTracker::new(...)`. 

`ConnectionRateLimiter::is_allowed` explicitly only tracks IPs that have a rate-limiter record, and returns `true` for any IP it has not "seen" yet: [2](#0-1) . A record is only created via `register_connection`, which is called exclusively in `setup_connection` **after** the QUIC handshake completes: [3](#0-2) . Likewise, `overall_connection_rate_limiter.consume_tokens(1)` (the actual token deduction) only happens post-handshake at line 495 of the same function; the pre-handshake check in `run_server` only peeks at `current_tokens()` without consuming, so it never actually gets drained by incomplete handshakes.

This means an attacker who opens a connection and stalls the handshake (e.g., sends only the QUIC Initial packet and goes silent) never triggers either rate limiter, no matter how many times it repeats this from the same source IP. The only resource consumed pre-handshake is the `ClientConnectionTracker` slot, acquired synchronously in `run_server` via `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` [4](#0-3) , and held until `setup_connection`'s `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` (2 seconds) expires or the connection completes/fails [5](#0-4) , at which point the tracker is dropped and the slot freed.

`max_concurrent_connections()` is a single global capacity shared by all staked and unstaked peers combined — for `SwQos` it is `(max_staked_connections + max_unstaked_connections) * 5 / 4` [6](#0-5) , which with defaults `DEFAULT_MAX_STAKED_CONNECTIONS = 2000` and `DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000` [7](#0-6)  equals 5000. A single unstaked attacker IP — never blocked by the per-IP limiter since it never registers — can open up to 5000 concurrent stalled handshakes and keep refreshing them (issuing a new one every &lt;2s as old ones time out) to permanently keep the pool saturated. Once saturated, `ClientConnectionTracker::new` fails for every other incoming connection attempt (staked or unstaked, from any IP), causing them to be refused via `incoming.refuse()` and counted in `refused_connections_too_many_open_connections` [4](#0-3) .

### Impact Explanation
This is a low-cost, single-source, unprivileged denial of service against the leader's TPU QUIC ingress: legitimate staked and unstaked transaction/vote traffic is refused at the accept stage while the attacker's half-open connections occupy the shared capacity, matching the "unstaked connection/stream limits enforced per source" invariant violation and "TPU ingress halted" impact described. This falls under the Agave bug-bounty "loss of liveness"/network DoS category, since a single IP (well below botnet scale) can starve the entire connection-acceptance pipeline.

### Likelihood Explanation
Feasibility is high: the attacker only needs to control packet pacing of its own QUIC client (send Initial, withhold the final handshake flight), open up to `max_concurrent_connections` (5000 by default) parallel attempts from one IP, and refresh them faster than the 2-second `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`. No staked identity, special privileges, or IP spoofing is required — this is exactly the scenario the code's own comment anticipates ("before a peer has asserted control over their ip address...") but the per-IP mitigation (`rate_limiter.is_allowed`) is ineffective against it because it is keyed off a post-handshake registration event.

### Recommendation
Track and rate-limit *in-flight, unverified* connection attempts per source IP (not just post-handshake registrations) — e.g., increment a per-IP outstanding-attempt counter before `incoming.accept()` and enforce a small per-IP cap on the number of pending/unverified handshakes, independent of `register_connection`. Alternatively, reserve only a fraction of `max_concurrent_connections` for unverified attempts, and/or shrink `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` for the acquired-but-unverified state.

### Proof of Concept
Integration test plan (extends `streamer/src/nonblocking/quic.rs` test utilities, e.g. `setup_quic_server`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_stalled_handshake_flood_starves_legit_connections() {
    let SpawnTestServerResult { server_address, stats, cancel, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig::default());

    // 1. From a single source IP, open `max_concurrent_connections` raw UDP
    //    sockets/quinn `Connecting` handles and send only the Initial packet,
    //    never completing the handshake (drop/never poll to completion).
    let stalled: Vec<_> = (0..EXPECTED_MAX_CONCURRENT_CONNECTIONS)
        .map(|_| start_half_open_connection(&server_address))
        .collect();

    // 2. Attempt a legitimate, fully-completing connection from a different
    //    (or same) IP within QUIC_CONNECTION_HANDSHAKE_TIMEOUT.
    let legit = make_client_endpoint(&server_address, None).await;

    // Expected (failing) assertion demonstrating the vulnerability:
    // stats.refused_connections_too_many_open_connections stays bounded and
    // the legit connection succeeds promptly.
    assert!(legit_connection_succeeds_within(Duration::from_secs(1)));
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) == 0);

    cancel.cancel();
}
```
Expected result on vulnerable code: the legitimate connection is refused (`refused_connections_too_many_open_connections` increments) while the attacker's stalled connections continuously occupy the `ClientConnectionTracker` pool, and `stats.connection_rate_limited_per_ipaddr` never increments for the attacker's IP despite thousands of repeated attempts.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L331-379)
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

**File:** streamer/src/quic.rs (L46-48)
```rust
pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

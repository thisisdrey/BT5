### Title
QoS `ClientConnectionTracker` slot exhaustion via stalled QUIC handshakes bypassing peek-only rate limiters - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The shared `max_concurrent_connections()` pool of `ClientConnectionTracker` slots is allocated in `run_server` before the QUIC handshake completes and is held for the full `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s), while both the per-IP and global connection rate limiters only *peek* at token availability at accept time rather than consuming a token. Token consumption (`rate_limiter.register_connection` / `overall_connection_rate_limiter.consume_tokens`) only happens after a handshake successfully completes in `setup_connection`, so an attacker who never completes the handshake never gets rate-limited and can repeatedly re-acquire tracker slots from a single IP, exhausting the shared capacity for all other connecting peers.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:331-379`), for every incoming connection attempt the server:
1. Checks `overall_connection_rate_limiter.current_tokens() == 0` — a non-consuming peek [1](#0-0) .
2. Checks `rate_limiter.is_allowed(&ip)` — also a non-consuming peek that returns `true` if tokens exist or if the IP has never been "registered" [2](#0-1)  and [3](#0-2) .
3. Only then allocates a `ClientConnectionTracker` slot from the shared, IP-agnostic pool bounded by `qos.max_concurrent_connections()` [4](#0-3) , which for `SwQos` equals `(max_staked_connections + max_unstaked_connections) * 5 / 4` [5](#0-4) .

The tracker slot is then held in `setup_connection` for `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) while awaiting the handshake future [6](#0-5) . The actual rate-limiter token consumption only occurs *after* a successful handshake, via `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` [7](#0-6) .

Because an attacker deliberately stalls the handshake (e.g., delays sending the QUIC Handshake packet after Initial) so it never completes, `register_connection`/`consume_tokens` are never invoked, and no rate-limiter state is ever consumed for that attempt. The peek-only `is_allowed`/`current_tokens` checks on the *next* attempt from the same IP therefore still pass, since nothing was decremented. This lets a single unstaked attacker IP repeatedly acquire fresh `ClientConnectionTracker` slots, each held for ~2 seconds, and by pipelining enough parallel stalled handshakes, occupy the entire `max_concurrent_connections()` pool continuously. Once the pool is full, `ClientConnectionTracker::new` fails for every other incoming connection (staked or unstaked, from any IP), incrementing `refused_connections_too_many_open_connections` and calling `incoming.refuse()` [4](#0-3) , denying legitimate TPU connection admission.

The comment in `run_server` states the intended mitigation policy is "limit duration of in-flight connection attempts with a timeout" plus "protect against connection attempt bursts with a global rate-limiter" and "rate-limit abusive peers by (control-asserted) ip" [8](#0-7) , but the rate limiters are only enforced on *completed* handshakes, leaving the shared tracker pool unprotected against incomplete/stalled handshake floods from a single source.

### Impact Explanation
This is a QoS/connection-admission evasion: a single unstaked, unprivileged attacker can deny TPU QUIC connection acceptance to all other senders (including staked, legitimate leader-bound traffic) for the duration of the attack by monopolizing the shared, IP-agnostic `max_concurrent_connections()` capacity using resource-cheap stalled handshakes. This matches the "QoS evasion" / connection-admission DoS bounty category, scoped to TPU connection acceptance denial.

### Likelihood Explanation
Feasible with basic tooling: any remote client can open raw UDP/QUIC Initial packets and simply withhold/delay the handshake completion; no stake, gossip presence, or special privileges are required. It is fully repeatable — as each stalled slot times out at 2 seconds, the attacker can immediately open a replacement stalled connection, sustaining full pool occupancy indefinitely with a manageable number of parallel sockets (`max_concurrent_connections()`, e.g. a few thousand by default configuration).

### Recommendation
Consume rate-limiter tokens (both per-IP and global) at admission time — before or concurrently with `ClientConnectionTracker` allocation — rather than only after a successful handshake, so incomplete/stalled handshakes are charged against the attacking IP's budget. Additionally, consider reserving/capping per-IP tracker-slot usage independent of the global pool so that no single source IP can consume more than a bounded fraction of `max_concurrent_connections()` while in the pre-handshake state.

### Proof of Concept
Integration test plan (extending existing `setup_quic_server`/`make_client_endpoint` test harness in `streamer/src/nonblocking/quic.rs` tests):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_stalled_handshake_exhausts_tracker_pool() {
    // Reduce QUIC_CONNECTION_HANDSHAKE_TIMEOUT via test hook or use a small
    // max_concurrent_connections config (max_staked=0, max_unstaked=N) so the
    // pool cap is small and reachable quickly.
    let SpawnTestServerResult { server_address, stats, cancel, join_handle, .. } =
        setup_quic_server(
            None,
            QuicStreamerConfig::default_for_tests(),
            SwQosConfig { max_unstaked_connections: 4, max_staked_connections: 0, ..Default::default() },
        );

    let pool_cap = (4usize) * 5 / 4; // matches max_concurrent_connections()

    // Attacker opens raw UDP sockets sending only QUIC Initial packets,
    // never completing the TLS handshake, up to pool_cap connections.
    let mut stalled_sockets = Vec::new();
    for _ in 0..pool_cap {
        let sock = tokio::net::UdpSocket::bind("0.0.0.0:0").await.unwrap();
        sock.connect(server_address).await.unwrap();
        // send a minimal/garbage QUIC Initial-like packet to trigger Incoming
        sock.send(&[/* crafted Initial packet bytes */]).await.unwrap();
        stalled_sockets.push(sock);
    }

    // give server time to allocate trackers for all attempts
    tokio::time::sleep(Duration::from_millis(200)).await;

    // Legitimate client attempts a real connection and should be refused
    let legit_conn = make_client_endpoint(&server_address, None).await; // expect failure/refuse
    assert!(legit_conn_result_is_refused(legit_conn));
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0);

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected assertion: `stats.refused_connections_too_many_open_connections` increments and the legitimate connection is refused while attacker sockets remain within the `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window, and repeating the stalled-connection cycle keeps the refusal persistent, demonstrating the shared pool is monopolized by a single source without triggering `connection_rate_limited_per_ipaddr`/`connection_rate_limited_across_all`.

### Citations

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

**File:** streamer/src/nonblocking/quic.rs (L471-475)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
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

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```

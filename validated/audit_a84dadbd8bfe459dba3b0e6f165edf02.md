### Title
Unstaked/staked peers starved of TPU capacity via handshake-churn exhaustion of the global `max_concurrent_connections` slot budget - ([File: streamer/src/nonblocking/quic.rs])

### Summary
In `run_server`, the per-IP rate limiter (`rate_limiter.is_allowed`) only rejects IPs that have already completed a prior handshake and been registered via `rate_limiter.register_connection`; it always allows connections from any IP it has never seen. `ClientConnectionTracker::new` enforces only a single *global* `max_concurrent_connections` budget (default `(max_staked_connections + max_unstaked_connections) * 5/4` in `SwQos::max_concurrent_connections`), shared by all in-flight, unauthenticated handshakes for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) each.

### Finding Description
The accept loop in `run_server` (`streamer/src/nonblocking/quic.rs:304-411`) processes each incoming QUIC `Initial` as follows:
1. `overall_connection_rate_limiter.current_tokens() == 0` check — a global token bucket refilling at `TOTAL_CONNECTIONS_PER_SECOND = 2500`/s with burst `MAX_CONNECTION_BURST = 1000` [1](#0-0) .
2. `rate_limiter.is_allowed(&incoming.remote_address().ip())` — this only consults existing per-IP token-bucket records; for any IP never previously *registered* (i.e., that never completed a handshake), it unconditionally returns `true` [2](#0-1) . Per-IP consumption only happens in `register_connection`, which is called from `setup_connection` **after** a successful handshake [3](#0-2) .
3. `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())` increments a single global `stats.open_connections` counter and rejects with `refused_connections_too_many_open_connections` only when that global counter exceeds `max_concurrent_connections` [4](#0-3) . This limit is not partitioned per source IP/peer — it is a single shared budget for the whole endpoint.
4. On success, `outstanding_incoming_connection_attempts` is incremented and `setup_connection` is spawned, which waits up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT = 2s` for the handshake to complete via `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` [5](#0-4) . Only when this future resolves (success, error, or timeout) is `stats.open_connections` decremented (via `ClientConnectionTracker::drop`) or `outstanding_incoming_connection_attempts` decremented.

Because the per-IP limiter never blocks unregistered IPs, and IP source addresses in UDP/QUIC Initial packets are attacker-controlled (spoofable, since the attacker deliberately never intends to complete the handshake and receive the reply), an attacker can present an effectively unlimited stream of "new" source IPs (or churn source ports on a single real IP, which also produces distinct `ConnectionTableKey`/global slot consumers). Each such Initial packet:
- passes the per-IP check unconditionally (new/never-registered IP),
- consumes one slot of the single global `max_concurrent_connections` budget for up to 2 seconds,
- is only throttled by the *overall* rate limiter, which is deliberately generous (2500/s sustained, 1000 burst) to accommodate legitimate steady-state connection churn, and is far larger than the global concurrent-connection budget divided by the 2-second handshake timeout window (default max_concurrent_connections = `(2000+2000)*5/4 = 5000`; at 2500/s the bucket can refill enough tokens within 2s to nearly saturate that same budget, and burst alone (1000) plus a further ~1s of steady 2500/s already exceeds it).

Once the global `open_connections` counter reaches `max_concurrent_connections`, every subsequent Initial — including from legitimate staked/unstaked peers completing real handshakes — is refused at `ClientConnectionTracker::new` and counted in `refused_connections_too_many_open_connections`, even though `overall_connection_rate_limiter` still has tokens and `rate_limiter.is_allowed` for the legitimate peer's IP still returns `true`. The code comment explicitly acknowledges this design tradeoff ("our connection/handshake abuse mitigation policy is one of shed fast... before a peer has asserted control over their ip address... employ... limit duration of in-flight connection attempts with a timeout... protect against connection attempt bursts with a global rate-limiter") — but the tradeoff means the *global* concurrency slot, not just the overall rate, is the actual bottleneck that an unauthenticated churn attack can saturate for the full 2-second window repeatedly, independent of per-IP enforcement.

### Impact Explanation
This is a scoped availability/DoS impact on the leader's TPU ingress path: legitimate unstaked and staked senders are refused connections (`refused_connections_too_many_open_connections`) and cannot submit transactions, even though neither the global nor per-IP rate limiters that are supposed to gate abusive traffic are exhausted. This matches the Agave bounty category of QoS evasion / resource-starvation DoS against validator ingress capacity, scoped to "starves legitimate senders of TPU capacity" as stated in the question.

### Likelihood Explanation
Feasible for any unprivileged remote attacker with the ability to send UDP packets to the TPU QUIC port and either (a) rotate ephemeral source ports/IPs, or (b) spoof source IPs (since the attacker never needs the reply — they intend to abandon the handshake). No stake, gossip, or validator control is required; a single attacker host (or small botnet) sending a steady flood of Initial packets that never complete the QUIC handshake within 2 seconds is sufficient to continuously occupy the shared `max_concurrent_connections` budget. The attack is trivially repeatable and self-sustaining as long as the flood continues.

### Recommendation
Partition or weight the global handshake-in-flight budget by source IP (or apply a stricter, per-IP cap on outstanding *unauthenticated* handshake attempts before `ClientConnectionTracker::new`'s global check), so that a churn of never-completing handshakes from many/spoofed IPs cannot consume more than a bounded fraction of the shared slot budget. Alternatively, reserve a portion of `max_concurrent_connections` exclusively for connections that have already passed retry/address validation (e.g., via QUIC's Retry mechanism, which would force the attacker to actually receive traffic at the claimed source IP before consuming a slot), rather than allowing a fully anonymous Initial to consume shared concurrency capacity for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.

### Proof of Concept
Integration test plan (extending existing `setup_quic_server`/`make_client_endpoint` test harness in `streamer/src/nonblocking/quic.rs` tests):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_handshake_churn_starves_legitimate_connections() {
    // Configure server with realistic small max_concurrent_connections for a fast test,
    // e.g. SwQosConfig { max_staked_connections: 50, max_unstaked_connections: 50, ..Default::default() }
    let SpawnTestServerResult { join_handle, receiver, server_address, stats, cancel } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig {
            max_staked_connections: 50,
            max_unstaked_connections: 50,
            ..Default::default()
        });

    // Attacker: repeatedly open QUIC client endpoints from many distinct source
    // ports/addresses and never complete/await the handshake (drop the future immediately),
    // firing faster than QUIC_CONNECTION_HANDSHAKE_TIMEOUT (2s) so slots stay pinned.
    let attacker_task = tokio::spawn(async move {
        for _ in 0..2000 {
            let ep = make_unbound_client_endpoint_with_fresh_port().await;
            let connecting = ep.connect(server_address, "localhost").unwrap();
            // do not await / immediately drop -> Initial sent, handshake abandoned
            drop(connecting);
            tokio::time::sleep(Duration::from_micros(200)).await; // > sustained rate
        }
    });

    // Legitimate client from a distinct, well-behaved IP/port completing full handshake+tx send.
    tokio::time::sleep(Duration::from_millis(500)).await; // let attacker saturate slots
    let legit_conn = make_client_endpoint(&server_address, None).await;
    let mut send_stream = legit_conn.open_uni().await;

    attacker_task.await.unwrap();
    cancel.cancel();
    join_handle.await.unwrap();

    // Assertion: legitimate connection should NOT be disproportionately refused
    // relative to the well-behaved baseline; a failing implementation shows
    // refused_connections_too_many_open_connections growing while
    // connection_rate_limited_across_all / connection_rate_limited_per_ipaddr remain low/zero
    // for the legitimate IP.
    assert!(send_stream.is_ok(), "legitimate peer should not be starved by handshake churn");
    assert_eq!(stats.connection_rate_limited_per_ipaddr.load(Ordering::Relaxed), 0);
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0);
}
```
Expected result on the current implementation: `refused_connections_too_many_open_connections` grows substantially and the legitimate client's handshake/stream is delayed or refused, while `connection_rate_limited_across_all` and `connection_rate_limited_per_ipaddr` remain near zero, demonstrating that the global concurrency slot — not the intended rate limiters — is the effective (and evadable) bottleneck.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-80)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;

/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
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

**File:** streamer/src/nonblocking/quic.rs (L471-476)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
```

**File:** streamer/src/nonblocking/quic.rs (L483-493)
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

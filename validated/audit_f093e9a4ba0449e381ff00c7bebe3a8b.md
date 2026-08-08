### Title
QUIC handshake churn evades per-IP `ConnectionRateLimiter` because `register_connection` only fires post-handshake, allowing global connection-slot exhaustion - ([File: streamer/src/nonblocking/quic.rs])

### Summary
An unprivileged attacker can flood the public TPU QUIC endpoint with UDP Initial packets and abandon each handshake before completion. Because `ConnectionRateLimiter::register_connection` is only invoked in `setup_connection` after a successful handshake, the per-IP token bucket is never charged, and the only real cap on outstanding handshake attempts is the globally-shared `ClientConnectionTracker`/`open_connections` counter bounded by `qos.max_concurrent_connections()`, which is not partitioned per source IP.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:304-411`), each incoming QUIC `Incoming` is subjected to two admission checks before a `ClientConnectionTracker` slot is allocated:
1. `overall_connection_rate_limiter.current_tokens() == 0` — a **non-consuming** read of a global `TokenBucket`.
2. `rate_limiter.is_allowed(&incoming.remote_address().ip())` — also **non-consuming**; for an IP never seen before it unconditionally returns `true` (`streamer/src/nonblocking/connection_rate_limiter.rs:34-40`).

Both real token consumption calls — `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` — happen only in `setup_connection` (`streamer/src/nonblocking/quic.rs:483,495`), which executes **after** the QUIC handshake future (`connecting.await`) resolves `Ok`. If the attacker never completes the handshake (aborts, drops packets, or the handshake exceeds `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`), execution falls into the `Err(e)` or timeout branch (`streamer/src/nonblocking/quic.rs:534-542`), and neither rate limiter is ever touched.

Meanwhile, before the handshake future is even polled, the loop already:
- increments `stats.total_incoming_connection_attempts` and `stats.outstanding_incoming_connection_attempts` (lines 342-344, 381-383),
- allocates a `ClientConnectionTracker` via `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` (line 371-372), which increments the **global** `stats.open_connections` counter and enforces a single shared ceiling (`streamer/src/nonblocking/quic.rs:236-252`) — not scoped per source IP.

Consequently, a single attacker IP can repeatedly trigger Initial-packet handshakes and abandon them, filling `open_connections` up to `max_concurrent_connections` and keeping `outstanding_incoming_connection_attempts` elevated for the duration of `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` each cycle — all while `register_connection` is never invoked, so `rate_limiter.is_allowed()` keeps returning `true` for that IP indefinitely. The doc comment at lines 331-340 states the intended defense-in-depth (timeout + global rate-limiter + per-IP limiter + per-peer cap), but the "global rate-limiter" only consumes tokens on completed handshakes too, so it does not bound attempt bursts either — it stays saturated at max capacity since it's never drained by failed attempts.

### Impact Explanation
Because the concurrency cap enforced by `ClientConnectionTracker` is global rather than per-IP, one attacker can occupy all `max_concurrent_connections` slots via handshake-timeout churn, causing the endpoint to refuse (`incoming.refuse()`) or ignore all new connections from legitimate stakers/senders (`stats.refused_connections_too_many_open_connections`). This is a connection-slot exhaustion / availability DoS against the TPU QUIC endpoint, falling under the QoS-evasion / resource-exhaustion bounty category, since the invariant that "per-IP QoS limits are charged based on connection attempts, not only completed handshakes" is violated.

### Likelihood Explanation
The attack requires only an unstaked client with the ability to send raw UDP packets to the leader's public TPU QUIC port — no stake, keys, or special access needed. It is fully repeatable: sending a burst of Initial packets and aborting/never completing each handshake is straightforward with a standard QUIC/UDP client or by directly crafting Initial packets. The attacker needs to sustain a rate exceeding `1 / QUIC_CONNECTION_HANDSHAKE_TIMEOUT * max_concurrent_connections` to keep the slot pool saturated, which is easily achievable from a single host given typical QUIC handshake timeout values (on the order of seconds) versus low per-attempt cost (a UDP packet).

### Recommendation
Charge the per-IP `ConnectionRateLimiter` (or an admission-time equivalent) based on *connection attempts* rather than only completed handshakes — e.g., call `rate_limiter.register_connection` (or a separate "attempt" bucket) at admission time in `run_server` before allocating a `ClientConnectionTracker` slot, so an IP that repeatedly aborts handshakes is throttled the same as one that completes them. Additionally, consider bounding `ClientConnectionTracker`/`outstanding_incoming_connection_attempts` per-IP (not only globally), and make `overall_connection_rate_limiter` consume a token at admission time (when `outstanding_incoming_connection_attempts` is incremented) rather than only after a successful handshake, so aborted-handshake floods actually drain the "global rate-limiter" as the code comment implies.

### Proof of Concept
Integration test plan (extending the pattern of `streamer/src/nonblocking/quic.rs` test helpers such as `setup_quic_server`/`make_client_endpoint`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_half_open_handshake_churn_evades_per_ip_limiter() {
    // Configure server with a small max_concurrent_connections (via SwQosConfig)
    // and a low max_connections_per_ipaddr_per_min so per-IP throttling would
    // normally engage quickly if it were charged on attempts.
    let SpawnTestServerResult { stats, server_address, cancel, join_handle, .. } =
        setup_quic_server(
            None,
            QuicStreamerConfig { max_connections_per_ipaddr_per_min: 2, ..QuicStreamerConfig::default_for_tests() },
            SwQosConfig { max_unstaked_connections: 4, ..Default::default() },
        );

    // Repeatedly open a raw UDP/quinn client endpoint to the server and drop it
    // immediately (before handshake completes), from the SAME source IP, many
    // more times than max_connections_per_ipaddr_per_min.
    for _ in 0..50 {
        let client = quinn::Endpoint::client("127.0.0.1:0".parse().unwrap()).unwrap();
        let connecting = client.connect(server_address, "localhost").unwrap();
        drop(connecting); // abort before handshake completes
    }

    tokio::time::sleep(Duration::from_millis(200)).await;

    // Assert: register_connection was never charged for this IP.
    assert_eq!(stats.connection_rate_limited_per_ipaddr.load(Ordering::Relaxed), 0);
    // Assert: many attempts were recorded/consumed slots, evidencing churn.
    assert!(stats.total_incoming_connection_attempts.load(Ordering::Relaxed) >= 50);
    // Assert: outstanding attempts eventually drain to 0 (bounded), but a
    // legitimate concurrent connection attempt made *during* the churn window
    // is refused due to shared/global slot exhaustion:
    let legit = make_client_endpoint(&server_address, None).await; // should be refused/delayed
    assert!(stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed) > 0
        || stats.connection_setup_timeout.load(Ordering::Relaxed) > 0);

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected result confirming the bug: `connection_rate_limited_per_ipaddr` stays at 0 despite far exceeding `max_connections_per_ipaddr_per_min` attempts from one IP, while legitimate connections are starved/refused due to the shared `max_concurrent_connections`/`open_connections` ceiling being exhausted by the churn.
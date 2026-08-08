### Title
Per-IP and global QUIC connection rate limiters are only enforced *after* the expensive TLS/QUIC handshake completes, allowing a single unstaked IP to force many concurrent handshakes before being rejected - (File: streamer/src/nonblocking/quic.rs)

### Summary
In `run_server`, the fast pre-accept gate only *peeks* at rate-limiter state (`ConnectionRateLimiter::is_allowed` and `TokenBucket::current_tokens() == 0`), neither of which consumes tokens. The actual token-consuming checks (`ConnectionRateLimiter::register_connection` and `overall_connection_rate_limiter.consume_tokens(1)`) only run in `setup_connection` *after* the full QUIC/TLS handshake (up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` = 2s) has completed. This creates a TOCTOU window in which many parallel connection attempts from the same IP can pass the non-consuming peek and perform full handshake CPU work before the per-IP/global buckets actually reject them.

### Finding Description
The connection admission path is:
1. `run_server` loop (streamer/src/nonblocking/quic.rs:331-379): for every incoming attempt it checks `overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed(&ip)` — both are *read-only peeks*: `TokenBucket::current_tokens()` and `ConnectionRateLimiter::is_allowed` (`streamer/src/nonblocking/connection_rate_limiter.rs:34-40`) never decrement the bucket.
2. Only `ClientConnectionTracker::new(stats, qos.max_concurrent_connections())` (streamer/src/nonblocking/quic.rs:236-251) enforces a real, immediately-mutating counter, but it is a **global** cap shared by all peers/IPs, not per-IP.
3. If that passes, `incoming.accept()` is called and `setup_connection` is spawned, which performs the actual QUIC/TLS handshake (`connecting.await`, streamer/src/nonblocking/quic.rs:472).
4. Only *after* the handshake succeeds does `setup_connection` call the consuming operations `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` (streamer/src/nonblocking/quic.rs:483-508).

Because steps 1 and 4 are separated by the full duration of a handshake (which can take up to 2 seconds under `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`), and because the pre-check in step 1 never decrements anything, an attacker who fires many connection attempts from a single source IP within that window will have *all* of them pass the peek (since the per-IP bucket still shows tokens > 0 until the first one is registered) and proceed to full handshake computation. Only afterward, once each handshake finishes and `register_connection` actually decrements the bucket, will the excess attempts be rejected and closed. The intended “cap total connections per-peer/ip” burst limit (`max_connections_per_ipaddr_per_min * 10`) is therefore not actually a bound on concurrent in-flight *handshakes* for one IP — the real bound on concurrent handshake CPU work from one IP is the *global* `qos.max_concurrent_connections()` value (shared with all other peers) and the global `MAX_CONNECTION_BURST` (1000) overall-bucket capacity, both of which are also enforced via non-consuming peeks and consuming ops split the same way.

The code comment at streamer/src/nonblocking/quic.rs:332-340 acknowledges the general design tradeoff (per-IP identity can't be trusted before handshake to avoid spoofing amplification), but it does not address that the per-IP bucket itself is checked with a non-atomic peek-then-later-consume pattern, meaning the effective per-IP concurrent-handshake ceiling is far larger than the configured `max_connections_per_ipaddr_per_min * 10` burst — bounded only by whatever share of the *global* `max_concurrent_connections` / `MAX_CONNECTION_BURST` a single IP can grab before the first handshake completes.

### Impact Explanation
A single unstaked, unauthenticated remote IP can force the leader to perform many concurrent full QUIC/TLS handshakes (asymmetric CPU cost for the server vs. attacker) up to the shared global `max_concurrent_connections` / `MAX_CONNECTION_BURST` limits, rather than being constrained to the intended per-IP burst (`max_connections_per_ipaddr_per_min * 10`). This matches the “grossly underpriced pre-fee work” / QoS-evasion category: cheap, feeless packets force disproportionate handshake CPU work relative to what the per-IP rate limiter is supposed to permit, though it is still bounded by the global connection-concurrency ceilings rather than unbounded.

### Likelihood Explanation
Feasible with only network access to the TPU QUIC port and no stake: the attacker simply needs to fire parallel `Connecting` attempts within roughly the handshake window (up to 2 seconds) faster than earlier attempts complete and register. This requires no protocol violation, no spoofing, and no cryptographic material beyond a standard QUIC client handshake. Repeatable indefinitely, bounded per burst by the global concurrency ceiling.

### Recommendation
Reserve/decrement the per-IP and overall token-bucket capacity at the pre-handshake gate (i.e., call the consuming `register_connection`/`consume_tokens` equivalent before `incoming.accept()`), refunding the token if the handshake subsequently fails or times out, rather than deferring consumption until after the handshake completes. Alternatively, add a per-IP counter of *outstanding* (in-flight) handshake attempts, separate from the completed-connection bucket, and reject new attempts from an IP once its in-flight handshake count exceeds a small per-IP cap.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (integration-style test, extend existing test harness)
#[tokio::test(flavor = "multi_thread")]
async fn test_single_ip_burst_bypasses_per_ip_peek_before_handshake() {
    // Configure a small per-IP burst (e.g. limit=1, so allowed burst = 10)
    let SpawnTestServerResult { server_address, stats, cancel, join_handle, .. } =
        setup_quic_server(
            None,
            QuicStreamerConfig {
                max_connections_per_ipaddr_per_min: 1,
                ..QuicStreamerConfig::default_for_tests()
            },
            SwQosConfig::default(),
        );

    // Fire N (> allowed burst) parallel connection attempts from the SAME source IP
    // (loopback) within a tight window, before any of them can complete + register.
    let n = 50;
    let handles: Vec<_> = (0..n)
        .map(|_| tokio::spawn(async move { make_client_endpoint(&server_address, None).await }))
        .collect();
    for h in handles { let _ = h.await; }

    // Expected (bug): total_new_connections / handshake completions observed via stats
    // significantly exceeds max_connections_per_ipaddr_per_min * 10 (the intended per-IP burst),
    // because most connections completed the handshake before rate_limiter.register_connection
    // rejected them.
    assert!(
        stats.total_new_connections.load(std::sync::atomic::Ordering::Relaxed) as u64
            <= 10, // intended per-IP burst cap
        "more handshakes completed than the per-IP burst should allow"
    );

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected result if the vulnerability is present: the assertion fails, showing that the number of completed handshakes (`total_new_connections`, or equivalently `connection_setup_error`+`total_new_connections` minus rejects observed only via `connection_rate_limited_per_ipaddr` post-handshake) from a single IP exceeds the configured per-IP burst, demonstrating the CPU cost was already paid for connections later rejected by `register_connection`.
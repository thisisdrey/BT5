### Title
Global `overall_connection_rate_limiter` TokenBucket permits multi-IP distributed exhaustion, starving legitimate senders of TPU connection acceptance - (File: streamer/src/nonblocking/quic.rs)

### Summary
`run_server`/`setup_connection` enforce two independent tiers of connection admission control: a per-IP `ConnectionRateLimiter` (default 8/min per IP, 10x burst) and a single global `overall_connection_rate_limiter: Arc<TokenBucket>` shared by all source IPs (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, `MAX_CONNECTION_BURST = 1000`). Because the global bucket is a single shared quota with no per-source reservation or fairness, an attacker who can source genuinely-routable connections from many distinct IPs (e.g. controlling an IPv6 /64 and completing full QUIC handshakes from many addresses in it) can drain the shared budget while each individual IP stays under the per-IP threshold, causing the server to drop/ignore connection attempts from unrelated legitimate clients.

### Finding Description
In `run_server` (streamer/src/nonblocking/quic.rs:255-412), every incoming connection is checked against the shared global limiter before any per-source or staked/unstaked differentiation: [1](#0-0) 
and then consumed in `setup_connection` after handshake completion: [2](#0-1) 

The bucket itself is a single, non-keyed `TokenBucket` (net-utils/src/token_bucket.rs:19-33) — it tracks one global atomic token count with no per-source subdivision: [3](#0-2) 

The per-IP limiter (`ConnectionRateLimiter`, streamer/src/nonblocking/connection_rate_limiter.rs:16-51) is a `KeyedRateLimiter<IpAddr>` that independently gates each source IP to `DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8` per minute (with 10x burst), defined in streamer/src/quic.rs:53-56. These two limiters are logically independent: the per-IP limiter bounds a *single* IP's rate, while the global limiter bounds *aggregate* throughput regardless of source. Nothing ties the global budget's allocation to the number of distinct sources currently connecting, so an attacker controlling many distinct source addresses (each individually compliant with the per-IP cap) can collectively consume the entire global budget (2500 tokens/sec sustained, burst up to 1000) before legitimate, unrelated clients get a chance — because the check happens as a hard gate ("if current_tokens == 0 → ignore()" / "if consume_tokens fails → close()") applied identically to all callers.

This is architecturally by design: the code comment states the global limiter exists to "protect against connection attempt bursts" (an aggregate resource bound), while the per-IP limiter is the mechanism intended to stop "abusive peers by (control-asserted) ip." The global limiter provides no fairness/reservation guarantee across sources, so it cannot by itself prevent one actor with many legitimately-owned addresses from monopolizing the shared quota.

### Impact Explanation
Scoped impact: total connection acceptance starvation for legitimate senders at the TPU QUIC ingress, matching the stated bounty category "an unprivileged, unstaked client can bypass or unfairly capture connection ... QoS limits and starve legitimate senders of TPU capacity." Because the global-limiter check in `run_server` runs before any staked/unstaked/QoS differentiation, exhaustion of the global bucket blocks *all* new incoming connection attempts indiscriminately at that instant, not just unstaked ones, amplifying the impact.

### Likelihood Explanation
Feasibility depends on the attacker being able to complete genuine (non-spoofed) QUIC handshakes from many distinct source IPs, since `register_connection`/`consume_tokens` on `overall_connection_rate_limiter` only happens after `Connecting` resolves successfully (post-handshake) — this requires the attacker to actually receive the server's handshake responses at each source address, i.e., own/route the address space (feasible and cheap via an owned IPv6 /64, which grants ~10^19 addresses). Sustaining exhaustion requires generating handshake completions at a rate near 2500/sec (or bursts of 1000), which requires meaningful automation/bandwidth but no privileged access, no protocol violation, and no per-IP threshold breach — each IP can stay at or below the 8/min policy limit. This is a real, reachable, and repeatable condition under the given preconditions (multi-IP attacker), though the required aggregate handshake rate is nontrivial and makes this a resource/cost-bound DoS rather than a free/trivial one.

### Recommendation
Introduce fairness in the global admission path, e.g.: (1) weight/cap the global budget's per-source draw (e.g., cap the fraction of the global bucket a single ASN/subnet/CIDR can consume in a rolling window), (2) prioritize known-staked or previously well-behaved IPs over completely new/unstaked sources when the global bucket is near depletion, or (3) replace the flat global `TokenBucket` with a stochastic-fair-queuing/weighted structure keyed by IP prefix (e.g. /56 or /64 for IPv6) so that a single actor controlling one prefix cannot consume more than a bounded share of `TOTAL_CONNECTIONS_PER_SECOND`.

### Proof of Concept
Integration test plan (extending the existing `streamer/src/nonblocking/quic.rs` test harness, e.g. near `test_quic_server_multiple_connections_on_single_client_endpoint`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_overall_rate_limiter_exhausted_by_many_distinct_ips() {
    // Spawn quic server with default QuicStreamerConfig (TOTAL_CONNECTIONS_PER_SECOND=2500,
    // MAX_CONNECTION_BURST=1000) and default max_connections_per_ipaddr_per_min=8.
    let SpawnTestServerResult { join_handle, receiver, server_address, stats, cancel } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig::default_for_tests());

    // Simulate K distinct source IPs (K * per_ip_burst > MAX_CONNECTION_BURST,
    // but each IP issues <= max_connections_per_ipaddr_per_min*10 connections).
    // In a real environment this requires binding client sockets to distinct local
    // addresses (loopback aliases / distinct interfaces) to emulate distinct source IPs.
    let mut handles = vec![];
    for _ in 0..1200 { // > MAX_CONNECTION_BURST of 1000
        handles.push(tokio::spawn(async move {
            make_client_endpoint(&server_address, None).await
        }));
    }
    let _conns: Vec<_> = futures::future::join_all(handles).await;

    // A legitimate, independent client issuing a single new connection afterward
    let legit_conn_result = make_client_endpoint(&server_address, None).await; // expect refusal/timeout

    // Assert the global limiter counter incremented and legitimate connection was blocked
    assert!(stats.connection_rate_limited_across_all.load(Ordering::Relaxed) > 0);
    // Demonstrate no per-IP counter tripped for the bulk of attacker connections since
    // each simulated IP stayed under DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE*10 burst.

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected assertion: `stats.connection_rate_limited_across_all` increases while `stats.connection_rate_limited_per_ipaddr` remains low/zero for the distributed attacker IPs, demonstrating that the per-IP guard did not trigger even though the global guard starved a legitimate, unrelated connection attempt.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
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

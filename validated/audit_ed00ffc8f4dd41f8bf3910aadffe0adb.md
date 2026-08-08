### Title
Global QUIC connection admission rate limiter has no staked-reserved capacity, allowing distributed unstaked IP-churn floods to starve legitimate/staked connection admission - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The QUIC ingress path enforces a single shared `overall_connection_rate_limiter` `TokenBucket` (capacity `MAX_CONNECTION_BURST`, refill `TOTAL_CONNECTIONS_PER_SECOND` = 2500/s) across *all* incoming connections before any stake/pubkey information is known. An unprivileged attacker distributed across many source IPs, each individually staying below the per-IP `ConnectionRateLimiter` threshold, can collectively saturate this shared bucket and cause legitimate unstaked and even staked clients' connection attempts to be rejected.

### Finding Description
In `run_server` (streamer/src/nonblocking/quic.rs:277-281), a single global `TokenBucket` is created: `overall_connection_rate_limiter = TokenBucket::new(MAX_CONNECTION_BURST, MAX_CONNECTION_BURST, TOTAL_CONNECTIONS_PER_SECOND)` [1](#0-0) .

For every accepted incoming connection, the loop first checks `overall_connection_rate_limiter.current_tokens() == 0` and drops the connection if the global bucket is empty, before any per-IP or stake check [2](#0-1) . Only after that does it check the per-IP `ConnectionRateLimiter::is_allowed` [3](#0-2) .

After the QUIC handshake completes in `setup_connection`, the code again consumes from the same global bucket via `overall_connection_rate_limiter.consume_tokens(1)` (streamer/src/nonblocking/quic.rs:495-508), and only afterwards calls `qos.build_connection_context` / `try_add_connection`, which is where stake-aware admission (e.g. `SimpleQos::cache_new_connection`, staked connection table) actually happens [4](#0-3) . This means stake is completely unknown to, and unused by, the global rate limiter — the global token bucket is consumed strictly on a first-come-first-served basis regardless of whether the connecting peer is staked or unstaked.

Because `ConnectionRateLimiter` (streamer/src/nonblocking/connection_rate_limiter.rs:21-29) is keyed per-`IpAddr` with its own independent `TokenBucket` per key (via `KeyedRateLimiter`), an attacker who spreads connection attempts across many distinct source IPs (e.g. via NAT/ephemeral-port-simulated distinct source addresses, or genuinely many hosts) can keep every individual per-IP bucket under its `max_connections_per_ipaddr_per_min` limit while the aggregate connection rate across all those IPs exceeds `TOTAL_CONNECTIONS_PER_SECOND` (2500/s) plus the `MAX_CONNECTION_BURST` (1000) headroom. Since the global bucket has no reservation, quota, or weighting for staked identities, once it is drained, *every* subsequent connecting peer — staked or unstaked — is rejected with `CONNECTION_CLOSE_CODE_DISALLOWED` at `streamer/src/nonblocking/quic.rs:503-507`, regardless of their legitimate stake weight.

No existing check mitigates this: the per-IP limiter only bounds a single IP's contribution, and stake-based admission control (`QosController::try_add_connection`) is never reached because the global token bucket check happens first and unconditionally.

### Impact Explanation
This is a leader-wide ingress denial-of-service: while the global connection admission bucket is exhausted, staked validators' TPU QUIC connections to the current leader are rejected identically to unstaked flood traffic, since stake is not considered at the point where the global bucket is checked. This can prevent legitimate transaction ingress (including from staked validators) during the flood window, degrading network throughput/liveness for the affected leader slot(s) — falling under the "ingress/QoS DoS" bounty category, as it is a QoS evasion / DoS achievable purely with unauthenticated, unstaked QUIC connections.

### Likelihood Explanation
The attack requires only the ability to originate QUIC connection attempts from many distinct source IP addresses (no stake, no keys, no validator control needed) — a low-cost precondition for a distributed or NAT/cloud-based attacker with access to many source addresses. The attacker must keep the per-IP connect rate below `max_connections_per_ipaddr_per_min` (staying "under the radar" per-IP) while collectively exceeding `TOTAL_CONNECTIONS_PER_SECOND`≈2500/s + `MAX_CONNECTION_BURST`=1000; this is straightforward with a moderately sized IP pool (e.g., hundreds of IPs each below the per-minute threshold). The attack is repeatable for the duration the attacker can sustain distributed connections and requires no special network position beyond reaching the leader's public TPU/QUIC port.

### Recommendation
Reserve a portion of the global connection admission budget for known-staked peers, or move the global rate-limit check after stake is known (post-handshake pubkey lookup) so unstaked/staked traffic can be weighted or given separate buckets (e.g., separate `TokenBucket`s for staked vs. unstaked global admission, similar to how staked/unstaked connection tables and stream throttling are already separated elsewhere in the QoS layer). At minimum, size/refill the unstaked-facing global bucket independently from a staked-reserved bucket so a distributed unstaked flood cannot starve staked connection admission.

### Proof of Concept
Rust integration test plan (extending existing tests in `streamer/src/nonblocking/quic.rs`, e.g. near `test_quic_server_block_multiple_connections`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_global_rate_limiter_starves_staked_admission_under_distributed_ip_flood() {
    // Spawn quic server with SwQosConfig where staked_nodes contains a legit staked pubkey/IP.
    // Configure max_connections_per_ipaddr_per_min high enough that per-IP limiter never trips
    // for the flood pattern used below.

    // Simulate N distinct attacker IPs (e.g. by binding client endpoints to N different
    // loopback-aliased addresses, or by mocking remote_address() in a lower-level unit test
    // against ConnectionRateLimiter + TokenBucket directly), each issuing connections at a rate
    // below max_connections_per_ipaddr_per_min but with N * rate > TOTAL_CONNECTIONS_PER_SECOND
    // + MAX_CONNECTION_BURST within a short window.

    // Assert: overall_connection_rate_limiter.current_tokens() reaches 0.
    // Then attempt a connection from the legitimate staked client IP/pubkey and assert it is
    // rejected with CONNECTION_CLOSE_CODE_DISALLOWED / stats.connection_rate_limited_across_all
    // increments, proving staked connection admission was denied purely due to unstaked IP churn,
    // with no reserved capacity protecting it.
}
```
A lower-level unit test can directly instantiate `TokenBucket::new(MAX_CONNECTION_BURST, MAX_CONNECTION_BURST, TOTAL_CONNECTIONS_PER_SECOND)` and a `ConnectionRateLimiter`, drive `consume_tokens`/`register_connection` calls simulating many distinct `IpAddr`s each under the per-IP limit, and assert that `overall_connection_rate_limiter.consume_tokens(1)` eventually returns `Err(_)` even though every individual per-IP `ConnectionRateLimiter::is_allowed` check for a hypothetical new staked IP still returns `true` — demonstrating the shared, stake-blind exhaustion.

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

**File:** streamer/src/nonblocking/quic.rs (L493-519)
```rust
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

                stats.total_new_connections.fetch_add(1, Ordering::Relaxed);

                let mut conn_context = qos.build_connection_context(&new_connection);
                if let Some(cancel_connection) = qos
                    .try_add_connection(
                        client_connection_tracker,
                        &new_connection,
                        &mut conn_context,
                    )
                    .await
```

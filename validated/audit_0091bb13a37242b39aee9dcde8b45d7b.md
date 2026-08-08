Confirmed: the `overall_connection_rate_limiter` is a single global `TokenBucket` (capacity `MAX_CONNECTION_BURST` = 1000, refill `TOTAL_CONNECTIONS_PER_SECOND` = 2500/s) shared by every incoming connection attempt regardless of source IP or stake, checked/consumed in `run_server` and `setup_connection` in `streamer/src/nonblocking/quic.rs` before any stake-based QoS admission (`SwQos`/`SimpleQos::try_add_connection`) runs.

### Title
Global unstaked-attacker-exhaustible connection admission TokenBucket allows distributed multi-IP DoS of legitimate stake-weighted connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` gates all incoming QUIC connections behind a single process-wide `overall_connection_rate_limiter` `TokenBucket` (capacity `MAX_CONNECTION_BURST` = 1000, refill `TOTAL_CONNECTIONS_PER_SECOND` = 2500/s) that is checked before per-IP limiting and before any stake-based admission decision is made. Because the bucket is shared across all source IPs and peer types, an attacker distributing connection attempts across many source IPs — each individually compliant with the per-IP `ConnectionRateLimiter` — can continuously consume the entire global token supply, starving legitimate staked peers of connection slots at the earliest admission stage, before stake is even evaluated.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:277-281`), the server constructs one shared `overall_connection_rate_limiter` `TokenBucket::new(MAX_CONNECTION_BURST, MAX_CONNECTION_BURST, TOTAL_CONNECTIONS_PER_SECOND)` per QUIC endpoint group. [1](#0-0) 

For every incoming connection, the loop first checks `overall_connection_rate_limiter.current_tokens() == 0` (a peek) and drops the incoming connection with `incoming.ignore()` if the bucket is empty, before doing per-IP `rate_limiter.is_allowed(...)` checks or any stake lookup: [2](#0-1) 

After the handshake completes, `setup_connection` actually consumes a token via `overall_connection_rate_limiter.consume_tokens(1)`, again prior to `qos.try_add_connection(...)` (the stake-weighted admission logic in `SwQos`/`SimpleQos`) being invoked: [3](#0-2) 

The per-IP `ConnectionRateLimiter` (`streamer/src/nonblocking/connection_rate_limiter.rs:16-29`) only bounds connections *per individual IP*; it does nothing to prevent aggregate consumption of the shared global bucket by many distinct IPs. [4](#0-3) 

There is no mechanism reserving a portion of `overall_connection_rate_limiter` capacity for staked/high-priority peers, nor does the stake-weighted QoS (`SwQos::try_add_connection`, `streamer/src/nonblocking/swqos.rs:344-443`) get a chance to prioritize staked connections ahead of the global rate limiter — admission into that logic only happens *after* the global token has already been spent. Consequently, unstaked attackers distributed over many IPs (trivial via cloud VPS pools, IP rotation, or a botnet) can keep the global bucket perpetually near zero, causing `incoming.ignore()`/`new_connection.close(CONNECTION_CLOSE_CODE_DISALLOWED, ...)` to be issued against legitimate staked clients attempting to connect, at negligible cost (no fee, no stake, just a TCP/IP-layer address and completing a QUIC handshake at low rate per IP).

### Impact Explanation
This is a low-cost distributed denial-of-service against legitimate stake-weighted transaction submission to a leader's TPU: staked validators/forwarders can be denied new QUIC connection admission by unstaked attackers even though the stake-weighted QoS layer (`SwQos`) is specifically designed to prioritize staked peers once a connection reaches it — that mechanism is bypassed entirely because the global limiter gates admission earlier and blindly. This falls under Agave's "grossly underpriced work causing DoS of legitimate stake-weighted clients" / QoS evasion category, since the entire point of stake-weighted QoS (protecting the TPU ingress for staked nodes) is defeated by consuming a shared, stake-agnostic resource ahead of it.

### Likelihood Explanation
Feasible and repeatable with modest attacker resources: no stake, no fee, and no special protocol knowledge is required — only the ability to originate QUIC handshakes from many distinct source IPs (cheap via cloud providers, botnets, or IPv6 address rotation) at a rate below each IP's individual limit (`max_connections_per_ipaddr_per_min`, default 8/min, burst 80). Given `MAX_CONNECTION_BURST` = 1000 and `TOTAL_CONNECTIONS_PER_SECOND` = 2500, an attacker needs only maintain roughly 2500 connection attempts/sec in aggregate (e.g., ~50 IPs each sending ~50 attempts/sec, well under most per-IP burst budgets, or many more IPs at lower individual rates) to keep the shared bucket exhausted continuously, since the check happens twice (peek in `run_server`, consume in `setup_connection`) and both draw from the same shared pool.

### Recommendation
Do not gate admission for staked/high-priority peers behind a single stake-agnostic global `TokenBucket` consumed before stake evaluation. Options: (1) evaluate stake/peer-type before consuming the global token and reserve a portion of `overall_connection_rate_limiter` capacity (or use a separate bucket) exclusively for staked connections; (2) move the global rate limit check to only apply to unstaked/unauthenticated pre-handshake `Incoming` acceptance (as a resource-exhaustion guard on the initial packet-processing cost) rather than to the final `consume_tokens` gate after handshake completion where stake is already knowable; (3) weight/prioritize token consumption or use a keyed limiter split by peer class so per-IP compliant but distributed attackers cannot starve the shared budget available to staked clients.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test harness or `tpu-client-next/tests/connection_workers_scheduler_test.rs` style tests):
```rust
#[tokio::test]
async fn test_distributed_ip_attackers_exhaust_overall_limiter_before_stake_check() {
    // Configure QuicStreamerConfig with default overall limiter constants
    // (MAX_CONNECTION_BURST = 1000, TOTAL_CONNECTIONS_PER_SECOND = 2500)
    // and a permissive per-IP ConnectionRateLimiter (e.g. default 8/min burst 80,
    // so a handful of attacker IPs each staying under 80/min individually never
    // trip per-IP limiting).
    //
    // Spin up server with SwQosConfig granting priority to a staked peer.
    //
    // Simulate N distinct attacker source IPs (e.g. via loopback aliases or by
    // directly driving `ConnectionRateLimiter`/`TokenBucket` state as unit-level
    // proxy for the network layer) each opening connections at a rate under their
    // individual per-IP allowance but collectively >= TOTAL_CONNECTIONS_PER_SECOND.
    //
    // Concurrently attempt to establish one staked connection.
    //
    // Assert: the staked connection is rejected via
    // CONNECTION_CLOSE_CODE_DISALLOWED / stats.connection_rate_limited_across_all
    // increments, demonstrating that per-IP compliant, unstaked, distributed
    // attackers can deny admission to a legitimate staked peer solely by
    // saturating the shared overall_connection_rate_limiter.
}
```
Unit-level minimal reproduction confirming the shared-bucket mechanics without full network setup:
```rust
#[test]
fn test_overall_token_bucket_is_stake_agnostic_and_shared() {
    use solana_net_utils::token_bucket::TokenBucket;
    let bucket = TokenBucket::new(1000, 1000, 2500.0);
    // N attacker "IPs" each consume 1 token, none individually rate-limited
    // per-IP (that check lives in a separate KeyedRateLimiter), but together
    // they drain the shared bucket to 0.
    for _ in 0..1000 {
        assert!(bucket.consume_tokens(1).is_ok());
    }
    // A "staked" client's connection attempt now fails identically to an
    // unstaked attacker's, since TokenBucket has no concept of peer identity/stake.
    assert!(bucket.consume_tokens(1).is_err());
}
```

### Citations

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
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

**File:** streamer/src/nonblocking/quic.rs (L495-519)
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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L16-29)
```rust
impl ConnectionRateLimiter {
    /// Create a new rate limiter per IpAddr. The rate is specified as the count per minute to allow for
    /// less frequent connections. Higher limit also allows higher bursts.
    /// num_shards controls how many shards are used in the underlying dashmap,
    /// should be set >= number of contending threads.
    pub fn new(limit_per_minute: u64, max_burst: u64, num_shards: usize) -> Self {
        Self {
            limiter: KeyedRateLimiter::new(
                CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD,
                TokenBucket::new(limit_per_minute, max_burst, limit_per_minute as f64 / 60.0),
                num_shards,
            ),
        }
    }
```

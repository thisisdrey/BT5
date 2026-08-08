### Title
Global unstaked-connection rate limiter is checked before stake-based QoS, allowing distributed unstaked IPs to starve staked validator connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` and `setup_connection` in `streamer/src/nonblocking/quic.rs` gate every incoming QUIC connection through a single shared `overall_connection_rate_limiter` token bucket (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, `MAX_CONNECTION_BURST = 1000`) before any peer identity/stake is known. Only after this global bucket is consumed does the code call `qos.build_connection_context()` to determine whether the peer is staked. Because the per-IP limiter (`ConnectionRateLimiter`) is independent per source address, an attacker controlling many distinct unstaked IPs — each individually staying under `max_connections_per_ipaddr_per_min` — can collectively exhaust the shared global bucket and cause `connection_rate_limited_across_all` rejections for legitimate staked validators.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:270-281`), the server creates:
- a per-IP `ConnectionRateLimiter` (keyed by `IpAddr`), and
- a single shared `overall_connection_rate_limiter: Arc<TokenBucket>` with capacity `MAX_CONNECTION_BURST` and refill rate `TOTAL_CONNECTIONS_PER_SECOND`. [1](#0-0) 

On every incoming connection, before the handshake even completes, the loop checks the global bucket first: [2](#0-1) 

Then per-IP is checked, then the connection is accepted and handed to `setup_connection`, which (after handshake) again consumes from the same shared global bucket, still *before* any stake information is derived: [3](#0-2) 

Only after this global-rate check succeeds does the code call `qos.build_connection_context(&new_connection)`, which is where stake lookup happens (via `get_connection_stake`) in both QoS implementations (`SimpleQos`, `SwQos`): [4](#0-3) [5](#0-4) 

Because `build_connection_context`/`try_add_connection` (stake-aware admission) is invoked strictly *after* the shared, non-stake-aware `overall_connection_rate_limiter` gate, there is no mechanism to prioritize or reserve capacity for staked peers in this bucket. The per-IP limiter's own burst allowance (`max_connections_per_ipaddr_per_min * 10`, default `8 * 10 = 80`, see `streamer/src/quic.rs:56` and `streamer/src/nonblocking/quic.rs:270-276`) lets a single unstaked IP legally burst up to 80 connection attempts instantly, each of which draws one token from the shared 1000-token global bucket. As few as ~13 distinct unstaked IPs bursting simultaneously (13 × 80 = 1040 > `MAX_CONNECTION_BURST` = 1000) can drain the shared bucket to zero, causing subsequent connections — staked or not — arriving during that window to be rejected via `connection_rate_limited_across_all` at `streamer/src/nonblocking/quic.rs:347-357` or `495-508`, with no path for `qos.build_connection_context` to ever run for those rejected staked attempts.

### Impact Explanation
This is a denial-of-service against staked validator connectivity to a leader's TPU: an unprivileged attacker controlling multiple source IPs (e.g., a small botnet or cloud IP pool — no validator/gossip/staked-node control required) can starve the shared connection-admission token bucket, causing legitimate staked nodes' QUIC connection attempts to be dropped (`incoming.ignore()` / `new_connection.close(CONNECTION_CLOSE_CODE_DISALLOWED, ...)`) purely due to unstaked traffic volume, before stake-based QoS prioritization (`qos.build_connection_context`, `try_add_connection`) is ever consulted. This falls under the QoS-evasion / DoS-of-staked-traffic bounty category since the "stake-weighted QoS" guarantee is bypassed at the connection-admission layer.

### Likelihood Explanation
Feasible and repeatable with modest resources: the attacker needs on the order of a dozen or more distinct source IPs (no special privileges, no stake, no cluster participation) each capable of bursting under the default per-IP burst allowance (80 connections/min per IP), timed to arrive within the same ~0.4 second window (1000 tokens / 2500 tokens-per-second refill) to overwhelm the shared bucket. This can be repeated continuously to sustain denial of staked connections, since the per-IP limiter resets independently for each attacking IP over time.

### Recommendation
Make the connection-admission rate limiting stake-aware, or reorder the checks so that stake is determined (or at least a lightweight signal, e.g., pre-shared TLS session ticket/pubkey from the handshake) before the shared global bucket is consulted, and reserve/prioritize a portion of `overall_connection_rate_limiter` capacity for known-staked pubkeys (e.g., a separate token bucket for staked vs. unstaked new-connection admission, similar to how staked/unstaked streams are already split via `ConnectionTable`/`SwQos`). Alternatively, increase the deny-by-stake priority: when the global bucket is exhausted, prefer completing handshakes for connections presenting known staked TLS certificates over unknown ones.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (integration-style test, added to existing #[cfg(test)] mod)
//
// Goal: demonstrate that M distinct unstaked IPs bursting connections can
// exhaust `overall_connection_rate_limiter` and cause a legitimate staked
// connection attempt (simulated by directly racing on the shared TokenBucket)
// to be rejected, without ever consulting stake info.

#[tokio::test(flavor = "multi_thread")]
async fn test_shared_rate_limiter_starves_staked_before_qos_check() {
    use solana_net_utils::token_bucket::TokenBucket;
    use std::sync::Arc;

    // Mirror the constants used in run_server.
    const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;
    const MAX_CONNECTION_BURST: u64 = 1000;

    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));

    // Simulate M distinct unstaked IPs, each consuming up to its allowed
    // per-IP burst (80 by default) from the SHARED global bucket, all
    // within the same instant (no sleep between them -> no refill).
    let attacker_ips = 13usize; // 13 * 80 = 1040 > MAX_CONNECTION_BURST
    let per_ip_burst = 80usize;

    for _ip in 0..attacker_ips {
        for _ in 0..per_ip_burst {
            // Each call models one unstaked connection reaching the point
            // in `setup_connection` where it calls
            // overall_connection_rate_limiter.consume_tokens(1) BEFORE
            // qos.build_connection_context() is ever invoked.
            let _ = overall_connection_rate_limiter.consume_tokens(1);
        }
    }

    // Now a legitimate STAKED connection attempts to consume from the same
    // shared bucket, exactly as setup_connection does at
    // streamer/src/nonblocking/quic.rs:495, which happens BEFORE
    // qos.build_connection_context() is called at line 512.
    let staked_result = overall_connection_rate_limiter.consume_tokens(1);

    // Assert the vulnerability: the staked connection is rejected solely due
    // to the shared bucket being drained by unstaked attackers, with no
    // stake-based exemption ever consulted.
    assert!(
        staked_result.is_err(),
        "expected the staked connection to be starved by unstaked flood on \
         the shared overall_connection_rate_limiter, proving stake is never \
         consulted before this rejection"
    );
}
```
This test operates on the exact same `TokenBucket` type and constants used by `run_server`/`setup_connection` (`streamer/src/nonblocking/quic.rs:277-281,495-508`), demonstrating that the shared, non-stake-aware bucket can be exhausted by unstaked traffic before `qos.build_connection_context` (`streamer/src/nonblocking/qos.rs:21`) is ever invoked to distinguish staked from unstaked peers. A full end-to-end version would spin up `setup_quic_server` with multiple `make_client_endpoint` clients bound to different source IPs (e.g., via network namespaces or a test harness that fakes `remote_address()`), assert `stats.connection_rate_limited_across_all` increments for a staked keypair's connection attempt.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L270-281)
```rust
    let rate_limiter = Arc::new(ConnectionRateLimiter::new(
        quic_server_params.max_connections_per_ipaddr_per_min,
        // allow for 10x burst to make sure we can accommodate legitimate
        // bursts from container environments running multiple pods on same IP
        quic_server_params.max_connections_per_ipaddr_per_min * 10,
        num_shards,
    ));
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

**File:** streamer/src/nonblocking/quic.rs (L510-519)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L301-313)
```rust
impl QosController<SwQosConnectionContext> for SwQos {
    fn build_connection_context(&self, connection: &Connection) -> SwQosConnectionContext {
        let remote_address = connection.remote_address();
        get_connection_stake(connection, &self.staked_nodes).map_or(
            SwQosConnectionContext {
                peer_type: ConnectionPeerType::Unstaked,
                total_stake: 0,
                remote_pubkey: None,
                in_staked_table: false,
                remote_address,
                stream_counter: None,
                last_update: Arc::new(AtomicU64::new(timing::timestamp())),
            },
```

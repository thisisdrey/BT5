### Title
Global QUIC connection rate limiter shared across staked and unstaked peers allows unstaked IP-churning attacker to starve all TPU connections - (File: streamer/src/nonblocking/quic.rs)

### Summary
The QUIC server enforces a single shared `overall_connection_rate_limiter` `TokenBucket` (capacity `MAX_CONNECTION_BURST` = 1000, refill `TOTAL_CONNECTIONS_PER_SECOND` = 2500/s) across *all* incoming connections regardless of stake, checked before and consumed after handshake with no per-peer-type exemption. An unstaked attacker who rotates through many distinct source IPs (e.g., an IPv6 /64 prefix) can open connections at up to the per-IP burst allowance from each IP, collectively exhausting the shared global bucket and causing `incoming.ignore()`/connection close for every subsequent connection attempt, including from staked validators.

### Finding Description
In `run_server`, each incoming connection is first checked against a single, process-wide `Arc<TokenBucket>` named `overall_connection_rate_limiter`, constructed once for the entire server regardless of peer stake: [1](#0-0) 

The check happens before any per-IP or per-peer-type identity is verified: [2](#0-1) 

and the token is *consumed* after handshake in `setup_connection`, again with no branch differentiating staked from unstaked or applying any weighting/reservation for staked peers: [3](#0-2) 

The only other gate is the per-IP `ConnectionRateLimiter`, which allows up to `max_connections_per_ipaddr_per_min * 10` burst per individual IP address: [4](#0-3) [5](#0-4) 

This per-IP limiter is keyed by IP only, so a fresh IP address always starts with a full bucket, `KeyedRateLimiter::consume_tokens` allocates a new prototype-cloned bucket on first sight of a key: [6](#0-5) 

Consequently, an attacker who controls a wide IP range (trivial to obtain with a single IPv6 /64 allocation) can distribute a flood of connection attempts across many distinct source addresses, each individually complying with the per-IP burst limit but collectively draining the single global `overall_connection_rate_limiter` bucket. Because the bucket is shared and unweighted by stake, this exhausts capacity for legitimate staked leaders' packets sent to the same TPU port, whose connections will also observe `current_tokens() == 0` and be dropped via `incoming.ignore()`, or fail `consume_tokens` in `setup_connection` and be closed with `CONNECTION_CLOSE_CODE_DISALLOWED`. There is no code path in `run_server`/`setup_connection`/`SwQos`/`get_connection_stake` that inspects peer stake *before* the overall rate limiter gate — stake is only known after handshake completion via TLS pubkey extraction, but by then the connection has already consumed (or been rejected by) the shared bucket regardless of stake.

### Impact Explanation
This is a complete, remotely-triggerable denial-of-service against a leader's TPU ingress: once the global bucket is depleted, all new QUIC connections — staked and unstaked alike — are refused at the transport layer before any transaction can be sent, effectively blinding the leader to incoming transactions from all senders, including staked validators and forwarders, for the duration of the flood. This matches Agave's "Network DoS" / availability-impacting bounty category, since it denies block-producing nodes the ability to accept transactions without requiring any stake, keys, or validator control.

### Likelihood Explanation
Preconditions are minimal and cheap: the attacker needs only unprivileged network access to the target's public TPU UDP/QUIC port and control of many source IPs (a single IPv6 /64 block, or trivially spoofable/rotatable addresses, suffices, since only the per-IP bucket differentiates by address, and it re-fills a fresh bucket per new IP). The attack is fully automatable, repeatable indefinitely (attacker simply keeps churning IPs faster than the global refill rate of 2500 tokens/sec), and requires no interaction with or knowledge of the leader's stake table, keys, or gossip participation.

### Recommendation
Reserve a portion of the global connection-acceptance capacity for staked peers, or maintain separate token buckets for staked vs. unstaked overall rates (mirroring the existing staked/unstaked split already used for `ConnectionTable`/`max_staked_connections`/`max_unstaked_connections` in `SwQos`). Since stake is only known post-handshake, one option is to defer the global-rate check until after `get_connection_stake` is resolved in `setup_connection`, and apply a stricter/separate bucket for unstaked connections while staked connections draw from a protected reserve or their own bucket, so an unstaked flood cannot deplete capacity earmarked for staked peers.

### Proof of Concept
Rust integration test plan (extending existing test harness in `streamer/src/quic.rs` test module, using `setup_swqos_quic_server`/`spawn_stake_weighted_qos_server`):
1. Configure a test server via `spawn_stake_weighted_qos_server` with a `StakedNodes` table containing one legitimate staked identity, and default `QuicStreamerConfig`/`SwQosConfig` (so `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST` apply, i.e., 1000 burst / 2500 per second).
2. Spawn `M` (e.g., 2000) `tokio` tasks, each using `make_client_endpoint_with_bind_ip` bound to a distinct loopback-adjacent or IPv6 address (simulating distinct attacker IPs), each opening a connection to `server_address` and immediately dropping/reconnecting to stay under `max_connections_per_ipaddr_per_min * 10` per IP.
3. Concurrently, have the legitimate staked identity attempt a connection via `make_client_endpoint` with the staked `Keypair`.
4. Assert that `stats.connection_rate_limited_across_all` (via `StreamerStats`) increments for the staked client's attempt, and/or that the staked client's connection is closed with `CONNECTION_CLOSE_CODE_DISALLOWED`/`CONNECTION_CLOSE_REASON_DISALLOWED`, demonstrating that unstaked IP churn exhausts `overall_connection_rate_limiter` and starves the staked connection — i.e., `overall_connection_rate_limiter.current_tokens() == 0` at the moment the staked connection is attempted.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L270-276)
```rust
    let rate_limiter = Arc::new(ConnectionRateLimiter::new(
        quic_server_params.max_connections_per_ipaddr_per_min,
        // allow for 10x burst to make sure we can accommodate legitimate
        // bursts from container environments running multiple pods on same IP
        quic_server_params.max_connections_per_ipaddr_per_min * 10,
        num_shards,
    ));
```

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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L21-29)
```rust
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

**File:** net-utils/src/token_bucket.rs (L303-316)
```rust
    pub fn consume_tokens(&self, key: K, request_size: u64) -> Result<u64, u64> {
        let (entry_added, res) = {
            let bucket = self.data.entry(key);
            match bucket {
                Entry::Occupied(entry) => (false, entry.get().consume_tokens(request_size)),
                Entry::Vacant(entry) => {
                    // if the key is not in the LRU, we need to allocate a new bucket
                    let bucket = self.prototype_bucket.clone();
                    let res = bucket.consume_tokens(request_size);
                    entry.insert(bucket);
                    (true, res)
                }
            }
        };
```

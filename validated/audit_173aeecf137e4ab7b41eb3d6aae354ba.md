### Title
Global QUIC connection rate limiter can be exhausted to deny legitimate peers - (File: streamer/src/nonblocking/quic.rs)

### Summary
The external report describes a bug class where a rate-limiting safeguard, meant to bound abuse, itself becomes a shared, exhaustible resource: an attacker cheaply drives the limiter's counter/limit to its cap, denying legitimate users of the same limiter. The `run_server` connection-accept loop in agave's QUIC TPU streamer contains an analogous shared, capped counter: `overall_connection_rate_limiter`, a single global `TokenBucket` consumed by every incoming connection regardless of source IP.

### Finding Description
In `streamer/src/nonblocking/quic.rs`, `run_server` creates one process-wide `TokenBucket` shared across *all* peers: [1](#0-0) 

For every incoming connection attempt, the server first checks (peek) and later consumes from this single global bucket in `setup_connection`, before/independently of the per-IP `ConnectionRateLimiter`: [2](#0-1) [3](#0-2) 

Because the global bucket (`MAX_CONNECTION_BURST` = 1000 tokens, refilled at `TOTAL_CONNECTIONS_PER_SECOND` = 2500/s) is shared cluster-wide per validator instance, any unprivileged/unstaked peer(s) that complete real QUIC handshakes (handshake completion is required before tokens are consumed, so classic IP-spoofing doesn't bypass this) can drive the bucket to zero. While a single IP is separately bounded by the per-IP `ConnectionRateLimiter`, that per-IP cap (`max_connections_per_ipaddr_per_min`, with a 10x burst allowance) is independent of, and can be exhausted well before, the global bucket's replenishment — and multiple unprivileged peers acting in concert (no stake or special role required) can keep the shared bucket saturated, causing `overall_connection_rate_limiter.current_tokens() == 0` to reject *every* incoming connection attempt, including from well-behaved, legitimate stake-weighted peers: [4](#0-3) 

This mirrors the reported bug class precisely: a rate-limiting mechanism designed to stop abuse (per-connection/per-IP throttling) has a single, capped, shared counter that becomes the actual attack surface — the attacker's only cost is bandwidth/compute to complete legitimate QUIC handshakes from a modest set of IPs, not any privileged role.

### Impact Explanation
When the global token bucket is starved, `incoming.ignore()` is called for *every* new connection attempt server-wide, regardless of stake or IP reputation, effectively DoS'ing the TPU QUIC ingestion path for all legitimate transaction senders on that validator until the bucket refills (bounded by `TOTAL_CONNECTIONS_PER_SECOND`). This can degrade transaction ingestion/availability cluster-wide if repeated against enough validators, which is a real (not purely theoretical) availability impact, satisfying the "QoS evasion / grossly underpriced pre-fee work" acceptance criteria — the attacker pays only handshake cost while denying service to others.

### Likelihood Explanation
Likelihood is moderate: it requires sustaining connection attempts from enough distinct IPs (to avoid being capped purely by the per-IP limiter) to consistently outpace the global refill rate of 2500/s and burst of 1000. This is achievable with a modest, low-cost IP pool (cloud instances/VPN egress addresses) and does not require stake, validator identity, or any privileged role — squarely an unprivileged-user-reachable QUIC/UDP streamer path.

### Recommendation
- Consider removing or substantially raising the shared global bucket's role as a hard gate on *legitimate* stake-weighted traffic, or exempt/prioritize connections from already-verified higher-stake identities once the QoS controller (`SwQos`) has classified the peer.
- Alternatively, shard the global limiter (e.g., per subnet or per stake-tier) so exhausting one shard doesn't block all peers, analogous to per-IP shard replenishment already used in `ConnectionRateLimiter`.
- Emit metrics/alerts when `connection_rate_limited_across_all` spikes so operators can react (comparable to the report's suggestion of an "emergency" override path), and consider auto-scaling the burst/refill parameters based on observed legitimate stake-weighted demand.

### Proof of Concept
1. Stand up N (e.g., 50-100) distinct source IPs, each staying under `max_connections_per_ipaddr_per_min` (so the per-IP `ConnectionRateLimiter` never blocks them individually).
2. From each IP, repeatedly open and complete real QUIC handshakes to the validator's TPU QUIC endpoint at a combined rate exceeding `TOTAL_CONNECTIONS_PER_SECOND` (2500/s) sustained, keeping `overall_connection_rate_limiter`'s token count at 0 (see `run_server`'s check at `streamer/src/nonblocking/quic.rs:347`).
3. Observe that legitimate peers' connection attempts are rejected via the `overall_connection_rate_limiter.current_tokens() == 0` branch (`streamer/src/nonblocking/quic.rs:346-357`) and the `connection_rate_limited_across_all` stat increments, even though those legitimate peers are individually well within their own per-IP limits.

**Note on completeness**: I was unable to fully inspect `ConnectionTable`'s eviction/`try_add_connection` logic (the file `streamer/src/nonblocking/connection_table.rs` referenced in `swqos.rs` was not resolvable in the index, and grep for its definition returned only usage sites in `quic.rs`/`swqos.rs`/`qos.rs`/`simple_qos.rs`), so I could not fully assess whether an unstaked-peer connection-table eviction path offers an equally strong (or stronger) DOS analog. The finding above is based on the confirmed, reachable global-rate-limiter code path.

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

**File:** streamer/src/nonblocking/quic.rs (L326-369)
```rust
        if last_datapoint.elapsed().as_secs() >= 5 {
            stats.report(name);
            last_datapoint = Instant::now();
        }

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

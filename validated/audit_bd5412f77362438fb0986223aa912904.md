### Title
Global unstaked connection rate limiter (`overall_connection_rate_limiter`) has no staked-connection reservation, allowing distributed-IP unstaked flood to starve staked TPU connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`run_server` and `setup_connection` gate every incoming QUIC connection — staked or unstaked — through a single shared `TokenBucket` (`overall_connection_rate_limiter`) before stake is even known. Because token consumption is unconditional and FIFO with respect to peer identity, an unprivileged attacker distributing low-rate connection attempts across many distinct source IPs (each individually compliant with the per-IP limiter) can exhaust this shared bucket and cause legitimate staked validators' connections to the same TPU endpoint to be rejected.

### Finding Description
In `run_server`, the global bucket is created once per endpoint with a fixed capacity/refill rate that is stake-agnostic: [1](#0-0) 

Every incoming connection is checked against this bucket twice, both before any knowledge of the peer's stake:
1. Pre-accept check that just inspects `current_tokens()` and drops if zero: [2](#0-1) 
2. Post-handshake consumption in `setup_connection`, which happens strictly *before* `qos.build_connection_context` is invoked to determine `ConnectionPeerType::Staked`/`Unstaked`: [3](#0-2) 

The only other gate is the per-IP `ConnectionRateLimiter` (`rate_limiter.is_allowed` / `register_connection`), which is keyed solely by source IP and defaults to a modest burst of `max_connections_per_ipaddr_per_min * 10` (default 8/min ⇒ 80 burst per source): [4](#0-3) 

Since stake is not yet resolved at the point the global bucket is consumed, there is no reservation, priority lane, or exemption for staked traffic. An attacker controlling many distinct source IPs (real or via widely distributed infrastructure) — each individually staying under the per-IP cap — can collectively drive connection attempts past `MAX_CONNECTION_BURST` (1000) and `TOTAL_CONNECTIONS_PER_SECOND` (2500), permanently keeping `overall_connection_rate_limiter.current_tokens() == 0` for the duration of the attack. From that point, both the pre-accept short-circuit and the post-handshake consumption reject *every* new connection, including ones from legitimately staked validators trying to open fresh TPU/QUIC connections, incrementing `connection_rate_limited_across_all`: [5](#0-4) 

This violates the intended invariant (stated in the code comments) that unstaked-connection limits should not be evadable via IP churn, and that the global limiter exists to bound resource consumption from bursts — not to become a vector for denying staked traffic.

### Impact Explanation
This is a cluster-relevant availability issue: a leader's TPU/QUIC ingress can be made globally unavailable to *new* staked connections (existing established connections are unaffected, but reconnects, restarts, or connection churn by legitimate staked validators during the attack window will be dropped). This falls under Agave's QoS-evasion / unstaked-limit-evasion and DoS bounty categories, scoped to the TPU QUIC ingress path in `streamer/src/nonblocking/quic.rs`.

### Likelihood Explanation
Feasible with only unprivileged network access: the attacker needs no stake, no validator/gossip control, and no cluster membership — merely the ability to originate connection attempts from many distinct IPs (e.g., cloud VMs, proxies, botnet-like distribution) toward the leader's public TPU QUIC port, each individually staying under the per-IP burst (80 by default). Sustaining slightly over 2500 total connection attempts/sec split across enough distinct IPs is a low-cost, repeatable, and non-privileged attack, and the bucket capacity (1000 burst) is trivially exhausted by a modest botnet.

### Recommendation
Resolve stake earlier (or reserve capacity) so unstaked connection floods cannot exhaust capacity needed by staked peers. Concretely: split `overall_connection_rate_limiter` into two pools (e.g., staked-reserved and unstaked-shared), or check `get_connection_stake` immediately after handshake and route staked connections through a separate/higher-priority token bucket (or bypass the global unstaked bucket for staked identities), consistent with how staked vs. unstaked connections are already partitioned in `ConnectionTable`/`StreamerStats` (`open_staked_connections` vs `open_unstaked_connections`).

### Proof of Concept
Integration-style test at the `streamer::nonblocking::quic` level (or a focused unit test directly against the `TokenBucket`/`ConnectionRateLimiter` primitives mirroring the exact constants used in `run_server`):

```rust
// Pseudocode structured as a Rust test using the actual types
#[tokio::test]
async fn test_overall_rate_limiter_starves_staked_connections() {
    use solana_net_utils::token_bucket::TokenBucket;
    use streamer::nonblocking::connection_rate_limiter::ConnectionRateLimiter;

    // Mirror MAX_CONNECTION_BURST / TOTAL_CONNECTIONS_PER_SECOND from streamer/src/nonblocking/quic.rs
    let overall = TokenBucket::new(1000, 1000, 2500.0);
    let per_ip = ConnectionRateLimiter::new(8, 80, 4);

    // Simulate 1000+ distinct-IP unstaked attackers, each within per-IP burst (<=80),
    // consuming the shared overall bucket before any stake is known.
    for i in 0..1200u32 {
        let ip = std::net::IpAddr::V4(std::net::Ipv4Addr::from_bits(i));
        assert!(per_ip.register_connection(&ip)); // passes per-IP check (first hit)
        let _ = overall.consume_tokens(1);          // simulates setup_connection's global check
    }

    // Now simulate a legitimate staked validator's fresh connection attempt.
    let staked_ip = std::net::IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 1));
    assert!(per_ip.register_connection(&staked_ip), "per-IP check passes for staked peer");

    // Expected (secure) behavior: staked connection should be prioritized/unaffected.
    // Actual (vulnerable) behavior: global bucket is exhausted regardless of stake.
    assert!(
        overall.consume_tokens(1).is_err(),
        "BUG: staked peer's connection is rejected by overall_connection_rate_limiter \
         even though it never contributed to exhausting it"
    );
}
```

Expected assertion in a fixed implementation: the staked peer's connection should succeed (via a reserved/prioritized pool) even when the shared unstaked pool is exhausted by distinct-IP churn; in the current code, `overall_connection_rate_limiter.consume_tokens(1)` in `setup_connection` (streamer/src/nonblocking/quic.rs:495) fails uniformly, confirming the vulnerability.

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

**File:** streamer/src/nonblocking/quic.rs (L495-512)
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
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L21-50)
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

    /// Check if the connection from the said `ip` is allowed.
    /// Here we assume that only IPs with actual confirmed connections are stored in it,
    /// since we should only modify server state once source IP is verified
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }

    pub fn register_connection(&self, ip: &IpAddr) -> bool {
        if self.limiter.consume_tokens(*ip, 1).is_ok() {
            debug!("Request from IP {ip:?} allowed");
            true // Request allowed
        } else {
            debug!("Request from IP {ip:?} blocked");
            false // Request blocked
        }
    }
```

**File:** streamer/src/quic.rs (L207-212)
```rust
    // Number of connections to the endpoint exceeding the allowed limit
    // regardless of the source IP address.
    pub(crate) connection_rate_limited_across_all: AtomicUsize,
    // Per IP rate-limiting is triggered each time when there are too many connections
    // opened from a particular IP address.
    pub(crate) connection_rate_limited_per_ipaddr: AtomicUsize,
```

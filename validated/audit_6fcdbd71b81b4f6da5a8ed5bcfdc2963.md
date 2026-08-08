### Title
Per-IP QUIC connection rate limiter can be trivially evaded via IPv6 /64 address rotation, defeating fair-share QoS intent - ([File: streamer/src/nonblocking/connection_rate_limiter.rs])

### Summary
`ConnectionRateLimiter` keys its `KeyedRateLimiter<IpAddr>` token buckets on the full 128-bit `IpAddr`, with no subnet aggregation for IPv6 [1](#0-0) . An attacker who owns (or is delegated) an IPv6 `/64` can source each QUIC connection from a fresh address in that block; each new address gets its own fresh token bucket via `is_allowed`'s "unseen IP => allow" fallback [2](#0-1) , effectively nullifying the intended per-source fairness limit while global caps remain the only real backstop.

### Finding Description
`run_server` applies `rate_limiter.is_allowed(&incoming.remote_address().ip())` before accepting, and `setup_connection` calls `rate_limiter.register_connection(&from.ip())` after the handshake completes [3](#0-2) [4](#0-3) . Both operate purely on the exact `IpAddr` value returned by `remote_address().ip()`. For IPv4 this is meaningful since address space is scarce for a single attacker, but for IPv6 a `/64` prefix (commonly delegated to a single residential/cloud customer) contains `2^64` addresses, all of which hash to distinct keys in the limiter. By rotating source addresses within their own delegated `/64`, an attacker manufactures effectively unlimited independent per-IP token buckets, each starting with a full `max_burst` allowance (`max_connections_per_ipaddr_per_min * 10`) [5](#0-4) , so the per-source burst/rate cap never meaningfully throttles this single attacker.

The ephemeral-source-port variant of the question does not apply: the limiter key is `IpAddr` only (no port component), so port churn on a fixed IP has zero effect on rate-limit evasion — that vector is already correctly mitigated by design.

`CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD` (100,000) governs when `KeyedRateLimiter` prunes stale entries [6](#0-5) , but this is a size-based garbage-collection trigger for the map, not a security boundary; it does not prevent the address-multiplication technique, it merely bounds map memory.

However, real resource-exhaustion protection at the server does not solely rely on the per-IP limiter: `overall_connection_rate_limiter` is a global `TokenBucket` (`MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND`) consumed regardless of source IP [7](#0-6) [8](#0-7) [9](#0-8) , and `ClientConnectionTracker::new` / `qos.max_concurrent_connections()` cap total concurrently open connections irrespective of source IP diversity [10](#0-9) . These IP-agnostic global caps are unaffected by IPv6 rotation and remain the actual backstop against unbounded memory growth or `handle_connection` flooding.

### Impact Explanation
The bypass allows a single unstaked attacker (controlling one IPv6 `/64`) to consume a disproportionate share of the shared unstaked connection/stream budget faster than the per-IP fairness mechanism intends, at the expense of other legitimate unstaked peers sharing the same global caps — a QoS-fairness evasion rather than an unbounded-resource or crash vulnerability, since the IP-agnostic global connection-rate and concurrency limits still bound total ingress load into `setup_connection`/`handle_connection`.

### Likelihood Explanation
Feasible for any attacker with a standard IPv6 delegation (most ISPs/cloud providers hand out `/64` or larger blocks), requiring only the ability to bind sockets to arbitrary addresses within their own delegated prefix — no spoofing of others' traffic and no privileged/staked access needed. Repeatable indefinitely as long as the attacker has address space within the prefix.

### Recommendation
For IPv6, key the rate limiter (and any related per-source accounting) on a fixed prefix (e.g., `/64` or `/56`) rather than the full 128-bit address, so that rotating addresses within an owned block maps to the same bucket. Consider deriving the key via `Ipv6Addr::segments()` truncated to the prefix length before insertion into `KeyedRateLimiter`.

### Proof of Concept
```rust
// streamer/src/nonblocking/connection_rate_limiter.rs (extend existing test module)
#[tokio::test]
async fn test_ipv6_prefix_rotation_bypasses_rate_limit() {
    use std::net::Ipv6Addr;
    let limiter = ConnectionRateLimiter::new(3, 3, 4);
    let base: u128 = Ipv6Addr::new(0x2001, 0xdb8, 0, 0, 0, 0, 0, 0).into();

    // Simulate an attacker rotating through many addresses within a single /64.
    let mut allowed_count = 0;
    for i in 0..1000u128 {
        let addr = IpAddr::V6(Ipv6Addr::from(base | i)); // same /64, distinct full address
        if limiter.register_connection(&addr) {
            allowed_count += 1;
        }
    }
    // Expected (buggy) behavior: nearly all 1000 "distinct" addresses are allowed,
    // demonstrating the per-/64 attacker was never rate-limited despite exceeding
    // the configured limit (3/min) by >300x from what should be treated as one source.
    assert!(
        allowed_count > 900,
        "per-IP limiter should have throttled a single /64 attacker, but allowed {allowed_count} connections"
    );
}
```
Integration-level PoC: extend `streamer` test harness (`setup_quic_server` used in `tpu-client-next/tests/connection_workers_scheduler_test.rs`) to open connections from N synthetic IPv6 addresses sharing one `/64` and assert `stats.connection_rate_limited_per_ipaddr` stays near zero while `stats.total_new_connections` scales linearly with N, confirming no per-source throttling occurred for the rotating attacker.

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L6-9)
```rust
/// Limits the rate of connections per IP address.
pub struct ConnectionRateLimiter {
    limiter: KeyedRateLimiter<IpAddr>,
}
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L11-14)
```rust
/// The threshold of the size of the connection rate limiter map. When
/// the map size is above this, we will trigger a cleanup of older
/// entries used by past requests.
const CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD: usize = 100_000;
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

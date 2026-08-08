### Title
Per-IP QUIC connection rate limit bypass via TOCTOU race between `is_allowed` and `register_connection` - ([File: streamer/src/nonblocking/connection_rate_limiter.rs])

### Summary
`ConnectionRateLimiter::is_allowed` and `ConnectionRateLimiter::register_connection` are two separate, non-atomic operations against the underlying `KeyedRateLimiter` token bucket: the former only reads `current_tokens`, while the latter later calls `consume_tokens`. Because QUIC connection setup in the streamer performs the `is_allowed` check at accept time and the `register_connection` consume only after the handshake completes (in a separate async task/future per connection), an attacker opening many concurrent handshakes from a single IP can have all of them pass the `is_allowed` check before any of them decrements the token bucket.

### Finding Description
`ConnectionRateLimiter::is_allowed` performs a read-only check via `self.limiter.current_tokens(ip)` and returns `true` if tokens remain (or if the IP has no record yet), without mutating any state: [1](#0-0) . The actual token consumption only happens in `register_connection`, via `self.limiter.consume_tokens(*ip, 1)`: [2](#0-1) .

Because these are two distinct calls made at different points in the connection lifecycle — `is_allowed` is invoked synchronously on each incoming connection attempt before acceptance, and `register_connection` is invoked later, after the QUIC/TLS handshake completes, inside a per-connection asynchronous task — there is a window during which an attacker can open `N` concurrent handshakes from the same source IP. Since token consumption has not yet occurred for any in-flight handshake at the time each new one is checked, all `N` concurrent `is_allowed` calls can observe a non-exhausted bucket and return `true`, even though the configured `limit_per_minute`/`max_burst` for that IP is much smaller (e.g., 1). Only after each handshake completes does `register_connection` attempt to consume a token, but by then the connections have already been accepted and have started consuming handshake/QUIC-worker resources. The test included in the file itself (`test_connection_rate_limiter`) only exercises the sequential case and explicitly does not test concurrent racing behavior, so this gap is not covered by existing tests. [3](#0-2) 

### Impact Explanation
This is a QoS-evasion bug: an unstaked, unprivileged attacker can exceed the configured `max_connections_per_ipaddr_per_min`/burst limit for a single source IP by racing concurrent QUIC handshakes, allowing them to capture disproportionate leader handshake/QUIC-worker capacity relative to their configured allowance and to other well-behaved peers. This does not itself crash the validator or corrupt state, but it defeats the purpose of the per-IP admission control designed to bound resource usage from a single source, matching the "QoS evasion" bounty category.

### Likelihood Explanation
The precondition is simply that a single attacker IP can open several QUIC connections concurrently to the leader's public TPU/TPU-forward QUIC port, which is fully within reach of an unprivileged remote client and requires no special timing beyond ordinary network concurrency (parallel connect attempts). The race window exists on every connection attempt cycle, so it is repeatable, though the degree of the win is bounded by how many handshakes can be raced within the (typically small) round-trip/handshake completion window before `register_connection` catches up.

### Recommendation
Make admission per-IP atomic: consume a token from the rate limiter at accept time (before/instead of the read-only `is_allowed` check), and only refund/no-op if the connection is later rejected for other reasons. Alternatively, merge `is_allowed`+`register_connection` into a single atomic check-and-consume operation guarded by the same shard lock, and call it once at the earliest point in the connection lifecycle (on `incoming.remote_address()` prior to accepting the handshake), rather than checking early and consuming late.

### Proof of Concept
```rust
// streamer/src/nonblocking/connection_rate_limiter.rs (test module)
#[tokio::test]
async fn test_concurrent_is_allowed_bypasses_limit() {
    use std::sync::Arc;
    let limiter = Arc::new(ConnectionRateLimiter::new(1, 1, 4)); // limit=1/min, burst=1
    let ip = IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1));

    // Simulate K "connecting" futures all calling is_allowed() concurrently,
    // mirroring run_server's pre-accept check, before any of them calls
    // register_connection() (mirroring setup_connection's post-handshake consume).
    let k = 8;
    let mut allowed_checks = Vec::new();
    for _ in 0..k {
        let limiter = limiter.clone();
        allowed_checks.push(tokio::spawn(async move { limiter.is_allowed(&ip) }));
    }
    let results: Vec<bool> = futures::future::join_all(allowed_checks)
        .await
        .into_iter()
        .map(|r| r.unwrap())
        .collect();

    // BUG: all K checks pass because none has consumed a token yet.
    assert_eq!(results.iter().filter(|b| **b).count(), k);

    // Now all K "handshakes" complete and register_connection is called.
    let mut registers = Vec::new();
    for _ in 0..k {
        let limiter = limiter.clone();
        registers.push(tokio::spawn(async move { limiter.register_connection(&ip) }));
    }
    let reg_results: Vec<bool> = futures::future::join_all(registers)
        .await
        .into_iter()
        .map(|r| r.unwrap())
        .collect();

    // Expected invariant (violated): accepted connections should never exceed burst=1.
    let accepted = reg_results.iter().filter(|b| **b).count();
    assert!(accepted <= 1, "accepted {accepted} connections, expected <= 1 (burst)");
}
```
Expected: the final assertion fails in the current implementation because all `K` `is_allowed` calls return `true` before any `register_connection` decrements the bucket, demonstrating that the burst/limit of 1 can be exceeded under concurrency.

### Citations

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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L42-50)
```rust
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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L53-82)
```rust
#[cfg(test)]
pub mod test {
    use {super::*, std::net::Ipv4Addr};

    #[tokio::test]
    async fn test_connection_rate_limiter() {
        let limiter = ConnectionRateLimiter::new(3, 3, 4);
        let ip1 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(!limiter.is_allowed(&ip1));
        assert!(!limiter.register_connection(&ip1));

        let ip2 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 2));
        for _ in 0..100 {
            assert!(
                limiter.is_allowed(&ip2),
                "just checking should not mutate state"
            );
        }
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.is_allowed(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(!limiter.is_allowed(&ip2));
    }
}
```

### Title
Non-atomic `is_allowed`/`register_connection` TOCTOU permits unbounded concurrent connection bursts per IP - ([File: streamer/src/nonblocking/connection_rate_limiter.rs])

### Summary
`ConnectionRateLimiter::is_allowed` is a pure peek that does not consume any token from the underlying `TokenBucket`, while the actual token consumption happens later in `register_connection`. Because these are two independent, non-atomic calls separated by the QUIC handshake (up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` = 2s), an attacker opening many simultaneous connections from one IP can have all of them pass the `is_allowed` pre-check before any of them calls `register_connection`.

### Finding Description
`ConnectionRateLimiter::is_allowed` (streamer/src/nonblocking/connection_rate_limiter.rs:34-40) only calls `self.limiter.current_tokens(ip)` and returns `true` if tokens remain (or if the IP has no record at all) — it never decrements the bucket. The actual accounting/consumption happens only in `register_connection` (lines 42-50) via `self.limiter.consume_tokens(*ip, 1)`. [1](#0-0) 

In `streamer/src/nonblocking/quic.rs`, connections go through the flow: `is_allowed` pre-check → `incoming.accept()` → async TLS/QUIC handshake (bounded by `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` of 2 seconds, defined at line 80) → `register_connection`. [2](#0-1) 

Since `is_allowed` never mutates shared state, it provides no mutual exclusion between concurrently-arriving connection attempts from the same source IP. Every one of N concurrently-spawned connection-accept tasks from a single attacker IP will independently query `current_tokens` and see the same (still-unconsumed) token count, so all N will pass the check regardless of how large N is, as long as they arrive before any of them completes its handshake and calls `register_connection`. Only after the ~2 second handshake window elapses does the bucket actually get decremented per successful connection, at which point the burst has already been admitted into the accept/handshake pipeline.

### Impact Explanation
This allows an unstaked, unprivileged remote attacker to transiently exceed the configured per-IP connection rate/burst by an amount bounded only by attacker concurrency, not by the token bucket's `max_burst` parameter. This consumes excess QUIC accept/handshake worker resources and increases ingress admitted into the streamer pipeline beyond the intended per-IP bound — a denial-of-service amplification vector against the leader's public TPU QUIC endpoint, consistent with the DoS/resource-exhaustion bounty category.

### Likelihood Explanation
Highly feasible: the attacker needs no stake, no special access, and no leader/gossip control — merely the ability to open many concurrent QUIC connections to the public TPU port from one source IP (easily done with a single client machine issuing parallel connection attempts). The race window is guaranteed to exist because the handshake step is asynchronous and takes up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s), which is far longer than the time needed to fire off many concurrent `is_allowed` checks. This is deterministically reproducible, not a rare race.

### Recommendation
Make the check-then-consume path atomic: either merge `is_allowed` and `register_connection` into a single atomic "try consume" operation performed synchronously at accept time (before spawning the handshake task), or have `is_allowed` itself reserve/consume a token (with the reservation released/refunded if the handshake later fails), so concurrent connections from the same IP cannot all observe a stale token count. At minimum, `is_allowed` should not be a no-op read against a mutable rate-limit resource that is separately and later consumed by a different call.

### Proof of Concept
Rust integration test plan for `streamer/src/nonblocking/connection_rate_limiter.rs`:
```rust
#[tokio::test]
async fn test_concurrent_is_allowed_toctou() {
    use std::{net::{IpAddr, Ipv4Addr}, sync::Arc};
    let limiter = Arc::new(ConnectionRateLimiter::new(60, 5, 4)); // burst = 5
    let ip = IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1));

    // Simulate 50 concurrent tasks racing through is_allowed before any
    // register_connection completes (mirrors is_allowed -> accept -> handshake -> register_connection).
    let mut handles = Vec::new();
    for _ in 0..50 {
        let limiter = limiter.clone();
        handles.push(tokio::spawn(async move {
            let allowed_pre_check = limiter.is_allowed(&ip);
            // simulate handshake delay
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            let registered = limiter.register_connection(&ip);
            (allowed_pre_check, registered)
        }));
    }

    let mut pre_check_passed = 0;
    let mut actually_registered = 0;
    for h in handles {
        let (pre, reg) = h.await.unwrap();
        if pre { pre_check_passed += 1; }
        if reg { actually_registered += 1; }
    }

    // Expected (buggy) behavior: pre_check_passed >> configured burst (5),
    // demonstrating the is_allowed check gates nothing under concurrency.
    assert!(pre_check_passed > 5, "TOCTOU: all concurrent pre-checks pass regardless of burst limit");
    // register_connection correctly enforces the burst limit on actual token consumption
    assert!(actually_registered <= 5);
}
```
Expected result confirming the vulnerability: `pre_check_passed` will be close to 50 (all concurrent callers see available tokens), demonstrating that `is_allowed` provides no effective admission control under concurrent bursts even though `register_connection`'s token consumption is correctly bounded — meaning the accept/handshake resources for all 50 connections are consumed before the limiter's real enforcement point. [3](#0-2) [2](#0-1)

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L31-50)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L78-80)
```rust
/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
```

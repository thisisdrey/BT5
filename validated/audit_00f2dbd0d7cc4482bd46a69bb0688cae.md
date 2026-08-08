### Title
Per-IP QUIC connection rate limiter keys strictly on `IpAddr` with no IPv6 /64 prefix aggregation, allowing address rotation to exhaust `overall_connection_rate_limiter` - (streamer/src/nonblocking/connection_rate_limiter.rs)

### Finding Description
`ConnectionRateLimiter::is_allowed` / `register_connection` rate-limit strictly by the exact `IpAddr` key stored in the underlying `KeyedRateLimiter<IpAddr>` [1](#0-0) , checking token availability per address before allowing/consuming a connection token [2](#0-1) . There is no CIDR/prefix-based aggregation of IPv6 addresses anywhere in this rate-limiting path — confirmed by the absence of any `/64`, subnet, or prefix-grouping logic in `net-utils/src/token_bucket.rs` or `streamer/src/nonblocking/connection_rate_limiter.rs`.

In `run_server` (`streamer/src/nonblocking/quic.rs`), each incoming connection is checked against a shared `overall_connection_rate_limiter: TokenBucket` (global, `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST`) and then against the per-IP `rate_limiter` [3](#0-2) ; after handshake, `rate_limiter.register_connection(&from.ip())` and `overall_connection_rate_limiter.consume_tokens(1)` are both charged in `setup_connection` [4](#0-3) .

Because an unprivileged remote attacker with an IPv6 /64 allocation (a routinely-provided, cheap ISP allocation, not requiring stake or gossip control) can bind outbound QUIC connections from any of ~2^64 addresses within that block, each synthesized address is a brand-new key in `KeyedRateLimiter<IpAddr>` and starts with a full token bucket. By issuing one connection per address (rotating addresses at will), the attacker never triggers the per-IP throttle (`DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE`), while every successful connection still consumes exactly one token from the shared `overall_connection_rate_limiter`. Because that bucket is refilled at a fixed global rate (`TOTAL_CONNECTIONS_PER_SECOND`) and shared across all senders (staked and unstaked), sustained connection churn from rotating IPv6 addresses can consume most/all of the global token budget, causing `overall_connection_rate_limiter.current_tokens() == 0` to reject legitimate senders' connection attempts at the top of `run_server`'s accept loop [5](#0-4) .

The per-IP limiter is explicitly designed to stop "churn or spoofed identity," but since QUIC connections require completed handshakes (return-routable source address ownership), the address-churn mechanic here is not spoofing in the traditional sense — it's abuse of legitimately-owned IPv6 address space rotation, which the current keying scheme (exact `IpAddr`, no prefix grouping) does not account for.

### Impact Explanation
This does not bypass sigverify, QoS stake-weighting, PoH, or corrupt block state — it is a resource-exhaustion / availability issue against the shared global connection-admission token bucket (`overall_connection_rate_limiter` / `TOTAL_CONNECTIONS_PER_SECOND`) in the QUIC TPU ingress path. Because `MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND` are shared across the whole endpoint regardless of stake, an attacker fully consuming this shared budget causes new connections from unstaked (and in principle staked, since the overall limiter check runs before per-IP/stake attribution) legitimate senders to be dropped at `incoming.ignore()`, degrading availability of the TPU port. This matches a QoS-evasion / availability-degradation category rather than a consensus or verification-bypass bug.

### Likelihood Explanation
Feasibility hinges entirely on the attacker's ability to originate large numbers of *distinct, reachable* IPv6 source addresses and complete a full QUIC handshake from each (the per-IP limiter is only charged in `setup_connection` after handshake completion) [6](#0-5) . This requires: (1) an IPv6 /64 (or larger) allocation actually routed to the attacker (common with many cloud/VPS/ISP providers), (2) sufficient bandwidth/CPU to complete many QUIC handshakes per second, and (3) the leader's default config (`DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8`, ten times burst) unmodified. Repeatability is straightforward — the attack is just "rotate source address, connect once" — but real-world impact depends on the leader's actual `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST` values and how much of that budget an attacker with realistic bandwidth can consume relative to the legitimate connection rate. Without being able to fully read the numeric constants for `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST` in this session, I cannot precisely quantify how large an attacker's IPv6-churn rate must be to meaningfully starve legitimate traffic; this is a scoped resource-contention concern rather than a full outage guarantee.

### Recommendation
Aggregate the per-IP rate limiter key for IPv6 addresses to a configurable prefix (e.g., /56 or /64) before indexing into `KeyedRateLimiter`, so that many addresses within the same allocation share one bucket, similar to how the invariant description already expects "cannot be evaded by connection churn." Additionally, consider weighting or partitioning `overall_connection_rate_limiter` capacity so a burst of address-diverse-but-related connections cannot monopolize the shared global admission budget (e.g., a secondary limiter keyed by /64 prefix, or reserving a slice of `TOTAL_CONNECTIONS_PER_SECOND` for previously-unseen prefixes).

### Proof of Concept
```rust
// streamer/src/nonblocking/connection_rate_limiter.rs (integration-style test)
#[tokio::test]
async fn test_ipv6_prefix_rotation_evades_per_ip_limiter() {
    let limiter = ConnectionRateLimiter::new(
        DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE, // 8
        DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE * 10,
        4,
    );

    // Synthesize N distinct IPv6 addresses within the SAME /64 allocation
    // (only the low 64 bits vary, as an attacker controlling one /64 could do).
    let base: u128 = 0x2001_db8_0000_0001_0000_0000_0000_0000;
    let n = 1000;
    let mut accepted = 0;
    for i in 0..n {
        let ip = IpAddr::V6(Ipv6Addr::from(base | i as u128));
        // each address is a brand-new key -> always allowed once
        assert!(limiter.is_allowed(&ip));
        if limiter.register_connection(&ip) {
            accepted += 1;
        }
    }

    // Per-IP limiter never throttles because no two connections share a key,
    // even though all n connections originate from a single /64 the attacker controls.
    assert_eq!(accepted, n, "per-IP limiter did not block any /64-rotated connection");
}
```

For the aggregate-starvation claim, an integration test would additionally spin up `run_server`/`setup_connection` with a small `TOTAL_CONNECTIONS_PER_SECOND`/`MAX_CONNECTION_BURST`, drive N rotated-IPv6 handshakes concurrently with a background task issuing normal connections from a fixed legitimate IP, and assert the legitimate IP's acceptance rate drops sharply (`connection_rate_limited_across_all` stat increments) once the attacker's connections have consumed `overall_connection_rate_limiter`'s tokens — using the existing `stats.connection_rate_limited_across_all` counter [7](#0-6)  as the assertion target.

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L6-9)
```rust
/// Limits the rate of connections per IP address.
pub struct ConnectionRateLimiter {
    limiter: KeyedRateLimiter<IpAddr>,
}
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L34-50)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L471-508)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
        match connecting_result {
            Ok(new_connection) => {
                debug!("Got a connection {from:?}");
                // now that we have observed the handshake we can be certain
                // that the initiator owns an IP address, we can update rate
                // limiters on the server
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

**File:** streamer/src/quic.rs (L207-212)
```rust
    // Number of connections to the endpoint exceeding the allowed limit
    // regardless of the source IP address.
    pub(crate) connection_rate_limited_across_all: AtomicUsize,
    // Per IP rate-limiting is triggered each time when there are too many connections
    // opened from a particular IP address.
    pub(crate) connection_rate_limited_per_ipaddr: AtomicUsize,
```

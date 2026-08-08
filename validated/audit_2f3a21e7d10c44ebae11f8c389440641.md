### Title
Global QUIC connection rate limiter allows unstaked flood to starve all incoming connections - (File: streamer/src/nonblocking/quic.rs)

### Summary
The QUIC server's global (protocol-wide) connection-rate token bucket, `overall_connection_rate_limiter`, is shared by every incoming connection regardless of source IP or stake, is checked and consumed *before* per-IP throttling, and is refilled at a fixed rate of 2500 tokens/sec with a 1000-token burst cap. This mirrors the Linea `RateLimiter` bug class: a single shared, protocol-wide budget that any unprivileged party can exhaust to deny the resource to everyone else.

### Finding Description
`run_server` constructs one global `TokenBucket` (`overall_connection_rate_limiter`) shared across all incoming connection attempts to the validator's TPU QUIC endpoint: [1](#0-0) [2](#0-1) 

For every incoming connection, the code checks the *global* bucket first, and only after that checks per-IP limits: [3](#0-2) 

The global bucket is consumed again in `setup_connection` once the handshake completes: [4](#0-3) 

Because this bucket is keyed by nothing (unlike `ConnectionRateLimiter`, which is per-`IpAddr` via `KeyedRateLimiter`), any unprivileged, unstaked client (or a small botnet of unstaked IPs, each individually complying with the per-IP limit of `DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8`/min) can collectively saturate the shared 2500 tokens/sec, 1000-token-burst budget: [5](#0-4) 

Once the global bucket is drained, `incoming.ignore()` is called for *every* subsequent connection attempt — including from legitimate staked validators/RPC clients trying to submit transactions — until the bucket refills: [6](#0-5) 

This is directly analogous to the Linea report: a single global, time-windowed budget shared by all users, exhaustible by a well-resourced but unprivileged actor, causing legitimate users' traffic (ETH withdrawals in Linea; transaction submission in agave) to be blocked for the remainder of the period.

### Impact Explanation
While each individual unstaked connection is still bound by per-IP and per-peer QUIC/stream throttles (`ConnectionRateLimiter`, `SwQos`/`SimpleQos` stream throttling), those per-IP limits do nothing to protect the *global* bucket, since it is checked/decremented independently and first in the pipeline. An attacker distributing connection attempts across many source IPs (trivial with cloud infrastructure or a botnet, no stake required) can keep the global bucket empty, meaning `incoming.ignore()` fires for essentially all connection attempts, including from staked/high-priority peers who have not yet established a connection. This degrades the validator's ability to accept new QUIC transaction-submission connections network-wide — a genuine node-level DoS against the TPU ingress path, not merely a self-inflicted throttling of the attacker's own traffic.

### Likelihood Explanation
Likelihood is moderate-to-high: opening a burst of ~1000+ concurrent QUIC handshake attempts from many distinct source IPs at line-rate is inexpensive and does not require stake, a whitelisted identity, or any privileged role — it only requires network bandwidth and IP diversity, which is well within reach of a "well-funded attacker" as in the original report. Existing per-IP and per-peer limits (`ConnectionRateLimiter`, `max_connections_per_ipaddr_per_min`) do not prevent this because the shared bucket has no per-source accounting.

### Recommendation
Consider removing reliance on a single global, unkeyed token bucket for connection admission, or supplementing it with fairness/quota mechanisms so that no single class of unstaked/low-reputation sources can exhaust the shared budget (e.g., reserve a fraction of the global bucket for staked/known peers, or make the "overall" limiter scale with per-IP consumption so that spreading load over many IPs doesn't bypass the intended limiting granularity). Any change should be validated with load tests simulating distributed low-stake connection floods to confirm staked/legitimate connection admission is preserved.

### Proof of Concept
1. Deploy a validator with default `QuicStreamerConfig` (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, `MAX_CONNECTION_BURST = 1000`).
2. From N distinct source IPs (N large enough that each IP's individual connection rate stays under `DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8`/min but the aggregate exceeds ~2500 connections/sec), continuously open new QUIC connections to the TPU/TPU-forwards endpoint.
3. Observe `stats.connection_rate_limited_across_all` incrementing in `run_server`, and legitimate staked client connection attempts being dropped via `incoming.ignore()` at [7](#0-6)  even though those legitimate clients individually comply with per-IP limits.

*Note: I was unable to fully verify whether staked peers are given a separate/prioritized admission path that bypasses `overall_connection_rate_limiter` entirely — the code as read shows the global check applied uniformly to all incoming connections regardless of eventual peer-type classification (which only happens later, post-handshake, in `qos.build_connection_context`). If such a bypass exists elsewhere and is confirmed, it would reduce the severity of this finding for staked traffic specifically, though unstaked/RPC-submitted transaction ingestion would remain affected.*

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-76)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;
```

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L331-369)
```rust
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

**File:** streamer/src/quic.rs (L50-56)
```rust
/// Limit to 500K PPS
pub const DEFAULT_MAX_STREAMS_PER_MS: u64 = 500;

/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```

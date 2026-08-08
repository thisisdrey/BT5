### Title
Global `overall_connection_rate_limiter` TokenBucket has no per-source sub-budget, allowing multi-IP Sybil exhaustion of shared connection-acceptance capacity - ([File: streamer/src/nonblocking/quic.rs])

### Finding Description
The QUIC accept path in `streamer/src/nonblocking/quic.rs` enforces two independent layers of admission control on incoming connections: a per-IP `KeyedRateLimiter`-backed check (keyed on source `IpAddr`, using `TokenBucket::consume_tokens`/`is_allowed` semantics) and a single shared `overall_connection_rate_limiter`, which is a single `TokenBucket` instance [1](#0-0) . `TokenBucket` tracks one scalar `tokens` counter with `AtomicU64`, refilled at a fixed rate and capped at `max_tokens`, with no notion of "who" is consuming [2](#0-1) . Because this bucket is global and additive across all callers, any set of distinct source identities (IP addresses) that each individually pass their own per-IP `KeyedRateLimiter` check can still, in aggregate, drive `consume_tokens` calls against the single shared bucket fast enough to deplete it within one refill window. Since per-IP enforcement (`KeyedRateLimiter`) and the global enforcement (`overall_connection_rate_limiter`) are separate, unrelated budgets — the per-IP limiter only bounds a single IP's rate, it does not allocate or reserve any fraction of the global budget to that IP — there is no mechanism preventing N different IPs from collectively consuming the entire global allowance, denying connection acceptance to subsequent legitimate unstaked/staked senders whose own per-IP buckets are still healthy.

### Impact Explanation
This is a TPU-wide connection-acceptance starvation: once the shared `overall_connection_rate_limiter` bucket is drained, `run_accept_loop`'s admission check rejects new QUIC connections regardless of the caller's stake or per-IP standing, until the shared bucket refills. This matches the "QoS bypass/starvation" bounty category — an unprivileged remote party can, purely by rotating source IPs (e.g., cheap IPv6 /64 enumeration), degrade TPU availability for all other senders without needing stake, keys, or any privileged position.

### Likelihood Explanation
Feasibility depends entirely on the ease of sourcing many distinct IPs, which is explicitly listed as a precondition the attacker is assumed to have (Sybil via IPv6 enumeration is cheap and widely available to unprivileged network clients). No stake, keys, or validator control are required — only opening many QUIC connections from many source addresses to the leader's public TPU port, which is within the defined attacker capability. The attack is repeatable every refill window and does not require winning any race beyond ordinary packet timing.

### Recommendation
Introduce a per-source cap on how much of the *global* budget any single IP (or /64 prefix, to address IPv6 enumeration) may consume per window, or replace the flat global `TokenBucket` with a hierarchical/fair-share limiter that reserves a minimum global allocation independent of the number of distinct source identities currently active. Alternatively, weight the global consumption by a decaying per-IP reputation/stake score so that connections from many previously-unseen IPs are proportionally more expensive against the shared budget than connections from established, well-behaved sources.

### Proof of Concept
Integration/fuzz test plan (extending the existing `net-utils/src/token_bucket.rs` test module style):
1. Construct one prototype `TokenBucket` matching `overall_connection_rate_limiter`'s configured capacity/refill rate, and a separate `KeyedRateLimiter` matching the per-IP limiter's configuration, mirroring the setup in `run_accept_loop`.
2. Spawn `N` simulated "connections," each with a unique `IpAddr`, each performing exactly one `KeyedRateLimiter::consume_tokens(ip, 1)` (expected `Ok`, since each IP is fresh and under its own per-IP budget) followed by one `overall_connection_rate_limiter.consume_tokens(1)` call.
3. Set `N` greater than `overall_connection_rate_limiter`'s `max_tokens`/burst capacity but with each IP only sending traffic within its own per-IP allowance.
4. Assert: (a) all `N` per-IP `consume_tokens` calls succeed (`Ok`), proving per-IP limiting never triggered; (b) `overall_connection_rate_limiter.current_tokens()` reaches 0 before all `N` connections are processed; (c) connections attempted after depletion — including from a legitimate, previously-unseen IP with full per-IP budget — receive `Err` from the global bucket, demonstrating denial of legitimate service purely via aggregate Sybil consumption of the shared budget.

### Citations

**File:** net-utils/src/token_bucket.rs (L19-33)
```rust
pub struct TokenBucket {
    new_tokens_per_us: f64,
    max_tokens: u64,
    /// bucket creation
    base_time: Instant,
    tokens: AtomicU64,
    /// time of last update in us since base_time
    last_update: AtomicU64,
    /// time unused in last token creation round
    credit_time_us: AtomicU64,
    /// Per-bucket time source for shuttle tests, replacing Instant::now().
    /// Shared via Arc so cloned buckets (e.g. in KeyedRateLimiter) use the same clock.
    #[cfg(feature = "shuttle-test")]
    pub time_us_override: Arc<AtomicU64>,
}
```

**File:** net-utils/src/token_bucket.rs (L72-94)
```rust
    /// Attempts to consume tokens from bucket.
    ///
    /// On success, returns Ok(amount of tokens left in the bucket).
    /// On failure, returns Err(amount of tokens missing to fill request).
    #[inline]
    pub fn consume_tokens(&self, request_size: u64) -> Result<u64, u64> {
        let now = self.time_us();
        self.update_state(now);
        match self.tokens.fetch_update(
            Ordering::AcqRel,  // winner publishes new amount
            Ordering::Acquire, // everyone observed correct number
            |tokens| {
                if tokens >= request_size {
                    Some(tokens.saturating_sub(request_size))
                } else {
                    None
                }
            },
        ) {
            Ok(prev) => Ok(prev.saturating_sub(request_size)),
            Err(prev) => Err(request_size.saturating_sub(prev)),
        }
    }
```

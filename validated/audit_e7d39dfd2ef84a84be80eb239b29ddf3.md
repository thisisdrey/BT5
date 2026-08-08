### Title
Recency-biased LRU eviction in `KeyedRateLimiter` lets a distinct-IP flood evict an actively-throttled IP's bucket, resetting it to "unseen" and bypassing per-IP connection throttling early - (File: net-utils/src/token_bucket.rs, streamer/src/nonblocking/connection_rate_limiter.rs)

### Finding Description
`ConnectionRateLimiter::is_allowed` treats an IP with no bucket entry as always allowed: `None => true, // if we have not seen IP, allow connection request` [1](#0-0) . Buckets are stored in a `KeyedRateLimiter<IpAddr>` created with a fixed `target_capacity` of `CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD = 100_000` [2](#0-1) .

When the underlying `DashMap` shard grows beyond `1.5x` of its target shard size, `maybe_shrink` evicts entries, keeping only the `target_shard_size` entries with the *most recent* `last_update` timestamp per shard (`entries.select_nth_unstable_by_key(..., Reverse(last_update))` then `.take(target_shard_size)`) [3](#0-2) . This is triggered opportunistically on any `consume_tokens` call once a countdown reaches zero [4](#0-3) .

An attacker who first exhausts their own IP's token bucket (via `register_connection`), then floods a large number of distinct source IPs (>100,000, feasible via an IPv6 block the attacker controls, since QUIC connection establishment requires actual return-path control, not blind spoofing) causes repeated `maybe_shrink` cycles. Because eviction favors the most-recently-updated entries, the attacker's already-exhausted bucket (last updated slightly earlier) can be evicted from its shard while the newer flood entries (each barely used, high remaining tokens) survive. Once the attacker's own entry is evicted, `current_tokens` returns `None` and `is_allowed` treats the IP as unseen, immediately allowing new connections — well before the `TokenBucket`'s natural refill rate would have restored equivalent tokens. This differs from a persistent/never-evicted bucket, which would gate the same IP based on `refill_rate` until `max_tokens` is reached (as confirmed by the refill semantics exercised in the unit tests `test_token_bucket_us_to_have_tokens` and `test_keyed_rate_limiter`) [5](#0-4) .

This lets an unstaked attacker who can source (not necessarily spoof, but control) a very large number of distinct IPs force premature resets of their own per-IP connection rate limit state, evading the `max_connections_per_ipaddr_per_min` throttle used to gate QUIC connection admission in `run_server` [6](#0-5)  and in the per-IP check at line 359 [7](#0-6) .

### Impact Explanation
This is a QoS-evasion bug scoped to connection-rate throttling: an unstaked, unprivileged attacker can bypass the intended per-IP connection rate limit ahead of schedule by forcing a targeted eviction via flooding the shared `KeyedRateLimiter` map past its cleanup threshold. The practical benefit is bounded — the attacker only regains "unseen" status for IPs whose entries get evicted, and must repeat the large-IP-flood technique per reset — but it is a genuine violation of the "per-IP limits enforced and cannot be evaded by cleanup-triggered state loss" invariant, matching the stated QoS-evasion bounty category (connection/stream/per-IP throttle bypass).

### Likelihood Explanation
Exploitability requires the attacker to originate connections from over 100,000 distinct source IPs with completed QUIC handshakes (return-path control needed, e.g., via an owned IPv6 prefix) fast enough to trigger multiple `maybe_shrink` cycles while the target IP's entry is comparatively "older." This is a nontrivial but realistic precondition (IPv6 allocations are cheap and match the question's stated precondition), and no code path currently prevents it — there is no floor on eviction eligibility tied to whether a bucket is currently exhausted/rate-limited. Repeatability is bounded by how quickly the attacker can generate fresh distinct-IP entries and by DashMap's per-shard shrink behavior, but the mechanism is deterministic given the code as written.

### Recommendation
Do not evict entries purely by recency when they represent an actively-throttled (exhausted or near-exhausted token) bucket; e.g., weight eviction by both `last_update` and remaining token level (prefer evicting fully-refilled/unused buckets first), or track eviction/"last-evicted" time per key so that a freshly-evicted key is treated conservatively (e.g., start with zero tokens rather than a full bucket) instead of `None => true`.

### Proof of Concept
```rust
// streamer/src/nonblocking/connection_rate_limiter.rs (add to test mod)
#[tokio::test]
async fn test_eviction_resets_exhausted_ip_early() {
    // small capacity to make eviction reachable in a test
    let limiter = ConnectionRateLimiter::new(/*limit_per_minute=*/1, /*max_burst=*/1, /*num_shards=*/2);
    let target_ip = IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1));

    // Exhaust the target IP's bucket.
    assert!(limiter.register_connection(&target_ip));
    assert!(!limiter.is_allowed(&target_ip)); // correctly throttled

    // Flood with many distinct fresh IPs to trigger maybe_shrink and evict target_ip's
    // (comparatively older) entry from its shard.
    for i in 0..600_000u32 {
        let ip = IpAddr::V4(Ipv4Addr::from_bits(i));
        limiter.register_connection(&ip);
    }

    // BUG: target_ip's entry may now be evicted, so is_allowed treats it as "unseen"
    // and returns true, even though its rate-limit window (60s) has not elapsed.
    assert!(
        !limiter.is_allowed(&target_ip),
        "target IP should remain rate-limited until natural refill, \
         not be reset to unseen via eviction"
    );
}
```
Expected (buggy) result: the final assertion fails because `is_allowed(&target_ip)` returns `true` after the flood-triggered eviction, demonstrating the premature per-IP throttle bypass.

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L11-29)
```rust
/// The threshold of the size of the connection rate limiter map. When
/// the map size is above this, we will trigger a cleanup of older
/// entries used by past requests.
const CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD: usize = 100_000;

impl ConnectionRateLimiter {
    /// Create a new rate limiter per IpAddr. The rate is specified as the count per minute to allow for
    /// less frequent connections. Higher limit also allows higher bursts.
    /// num_shards controls how many shards are used in the underlying dashmap,
    /// should be set >= number of contending threads.
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

**File:** net-utils/src/token_bucket.rs (L303-342)
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

        if entry_added {
            if let Ok(count) =
                self.countdown_to_shrink
                    .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |v| {
                        if v == 0 {
                            // reset the countup to starting position
                            // thus preventing other threads from racing for locks
                            None
                        } else {
                            Some(v.saturating_sub(1))
                        }
                    })
            {
                if count == 1 {
                    // the last "previous" value we will see before counter reaches zero
                    self.maybe_shrink();
                    self.countdown_to_shrink
                        .store(self.shrink_interval, Ordering::Relaxed);
                }
            } else {
                self.approx_len.fetch_add(1, Ordering::Relaxed);
            }
        }
        res
    }
```

**File:** net-utils/src/token_bucket.rs (L354-390)
```rust
    #[allow(clippy::arithmetic_side_effects)]
    fn maybe_shrink(&self) {
        let mut actual_len = 0;
        let target_shard_size = self.target_capacity / self.data.shards().len();
        if target_shard_size == 0 {
            return;
        }
        let mut entries = Vec::with_capacity(target_shard_size * 2);
        for shardlock in self.data.shards() {
            let mut shard = shardlock.write();

            if shard.len() <= target_shard_size * 3 / 2 {
                actual_len += shard.len();
                continue;
            }
            entries.clear();
            entries.extend(
                shard.drain().map(|(key, value)| {
                    (key, value.get().last_update.load(Ordering::SeqCst), value)
                }),
            );

            entries.select_nth_unstable_by_key(target_shard_size, |(_, last_update, _)| {
                Reverse(*last_update)
            });

            shard.extend(
                entries
                    .drain(..)
                    .take(target_shard_size)
                    .map(|(key, _last_update, value)| (key, value)),
            );
            debug_assert!(shard.len() <= target_shard_size);
            actual_len += shard.len();
        }
        self.approx_len.store(actual_len, Ordering::Relaxed);
    }
```

**File:** net-utils/src/token_bucket.rs (L494-550)
```rust
    #[test]
    fn test_keyed_rate_limiter() {
        let prototype_bucket = TokenBucket::new(100, 100, 1000.0);
        let rl = KeyedRateLimiter::new(8, prototype_bucket, 2);
        let ip1 = IpAddr::V4(Ipv4Addr::from_bits(1234));
        let ip2 = IpAddr::V4(Ipv4Addr::from_bits(4321));
        assert_eq!(rl.current_tokens(ip1), None, "Initially no buckets exist");
        rl.consume_tokens(ip1, 50)
            .expect("Bucket is initially full");
        rl.consume_tokens(ip1, 50)
            .expect("We should still have >50 tokens left");
        rl.consume_tokens(ip1, 50)
            .expect_err("There should not be enough tokens now");
        rl.consume_tokens(ip2, 50)
            .expect("Bucket is initially full");
        rl.consume_tokens(ip2, 50)
            .expect("We should still have >50 tokens left");
        rl.consume_tokens(ip2, 50)
            .expect_err("There should not be enough tokens now");
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            rl.current_tokens(ip1).unwrap() > 40,
            "We should be refilling at ~1 token per millisecond"
        );
        assert!(
            rl.current_tokens(ip1).unwrap() < 70,
            "We should be refilling at ~1 token per millisecond"
        );
        rl.consume_tokens(ip1, 40)
            .expect("Bucket should have enough for another request now");
        thread::sleep(Duration::from_millis(120));
        assert_eq!(
            rl.current_tokens(ip1),
            Some(100),
            "Bucket should not overfill"
        );
        assert_eq!(
            rl.current_tokens(ip2),
            Some(100),
            "Bucket should not overfill"
        );

        rl.consume_tokens(ip2, 100).expect("Bucket should be full");
        // go several times over the capacity of the TB to make sure old record
        // is erased no matter in which bucket it lands
        for ip in 0..64 {
            let ip = IpAddr::V4(Ipv4Addr::from_bits(ip));
            rl.consume_tokens(ip, 50).unwrap();
        }
        assert_eq!(
            rl.current_tokens(ip1),
            None,
            "Very old record should have been erased"
        );
        rl.consume_tokens(ip2, 100)
            .expect("New bucket should have been made for ip2");
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

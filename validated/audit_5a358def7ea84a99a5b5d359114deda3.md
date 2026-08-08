### Title
Per-IP QUIC connection rate limiter's LRU eviction resets depleted token buckets, enabling unprivileged bypass of connection-attempt throttling - (File: `net-utils/src/token_bucket.rs`)

### Summary
`KeyedRateLimiter`, used by `ConnectionRateLimiter` to throttle incoming QUIC connection attempts per source IP before any stake/pubkey verification, evicts "stale" entries using only `last_update` recency (LRU), discarding the bucket's depleted-token state entirely on eviction. Because the eviction is triggered lazily by insertion volume from *any* key, an unprivileged remote attacker can force their own (or a targeted) throttled bucket to be evicted and lazily recreated from the fresh `prototype_bucket` (full token count), bypassing the intended per-IP connection-rate limit — the same "stale accumulated per-key state is silently discarded/reset instead of being reconciled" defect class described in the reference report (where per-pool `_arbitrageProfits` were not cleared on removal, causing unfair distribution on re-add). Here the analogous flaw runs in the opposite direction: removal/recreation of a rate-limit key resets accounting state to the attacker's advantage rather than preserving it.

### Finding Description
`ConnectionRateLimiter::is_allowed`/`register_connection` in `streamer/src/nonblocking/connection_rate_limiter.rs` gate every incoming QUIC connection by IP address, via `KeyedRateLimiter<IpAddr>` backed by a `DashMap<K, TokenBucket>`: [1](#0-0) 

This check happens in the QUIC accept loop before any pubkey/stake is known, as the very first line of defense against connection floods: [2](#0-1) 

`KeyedRateLimiter::consume_tokens` lazily creates a bucket from `prototype_bucket` for unseen keys, and periodically calls `maybe_shrink()` once an insertion counter reaches `shrink_interval`: [3](#0-2) 

`maybe_shrink` performs per-shard LRU eviction purely by `last_update` recency, dropping any bucket that isn't in the most-recently-touched `target_shard_size` subset of its shard — it does not distinguish "not fully depleted" from "already exhausted" state; a heavily throttled (i.e., recently-updated, since consuming tokens bumps `last_update`) IP's bucket is *retained*, but the moment its updates stop being "recent enough" relative to newer keys flooding in, it is dropped along with all its accumulated consumption history: [4](#0-3) 

Once evicted, the very next connection attempt from that IP re-enters `Entry::Vacant` and gets a brand-new bucket cloned from `prototype_bucket` — i.e., a full quota, ignoring however much was previously consumed: [5](#0-4) 

This is structurally identical to the reported Salty bug class: a "removal" operation (LRU eviction here; unwhitelisting there) silently discards accumulated per-key accounting state instead of settling/preserving it, and the entity effectively "returns" (recreated bucket / re-whitelisted pool) with an unearned fresh allocation, undermining the invariant the accounting was meant to enforce (fair reward split there; bounded connection rate here).

### Impact Explanation
An unprivileged network attacker who controls or spoofs enough distinct source IPs (trivial from cloud/VPS pools, or via IP rotation) can drive high insertion volume into the `KeyedRateLimiter`, forcing `maybe_shrink` to fire repeatedly. Each shrink cycle evicts the least-recently-updated buckets in a shard, including targets that are simply not being hit constantly. By interleaving bursts against a specific IP with intervening bursts against many decoy IPs (to push the target out of the "recently updated" retained set, or simply by regenerating buckets that hit the vacant path), the attacker's own throttled IP entries get wiped and immediately replaced with a fresh, fully-topped-off `TokenBucket`, letting them keep issuing new QUIC connection attempts far beyond the configured per-IP `limit_per_minute`/`max_burst`. Since this check sits ahead of `ClientConnectionTracker` and full handshake processing in `run_server`, this enables an unprivileged peer to defeat the QUIC connection-attempt QoS mechanism designed specifically to bound resource consumption from unauthenticated peers, contributing to a connection-flood / resource-exhaustion DoS vector against the TPU/QUIC endpoint.

### Likelihood Explanation
Reachable by any unauthenticated network peer able to originate QUIC connection attempts from multiple source addresses — no stake, pubkey, or validator role required, and the shrink trigger (`shrink_interval`, derived from `target_capacity / 4`, with `target_capacity` = `CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD` = 100,000) is driven purely by attacker-controlled insertion volume, making it straightforward to trigger deliberately as opposed to only occurring under organic load.

### Recommendation
Do not treat LRU-based capacity eviction as equivalent to a legitimate quota reset: either (a) skip eviction for buckets that are not near-full (i.e., still actively rate-limiting, `current_tokens() < max_tokens`), (b) persist a decayed "penalty" or minimum cool-down independent of DashMap residency (e.g., a small independent last-violation timestamp map that is checked even after bucket eviction), or (c) shrink using a policy that prioritizes evicting buckets closest to `max_tokens` (least likely to be actively throttling) rather than pure recency, so an actively-throttled IP's state cannot be trivially wiped by unrelated key churn.

### Proof of Concept
1. Configure a validator with default `ConnectionRateLimiter::new(limit_per_minute, max_burst, num_shards)` used for QUIC connection admission (`streamer/src/nonblocking/quic.rs`, `rate_limiter.is_allowed`).
2. From IP `A`, exceed `max_burst` connection attempts so `A`'s bucket is depleted and `is_allowed` returns `false` (confirmed throttled).
3. From a large pool of distinct IPs `B1..Bn`, issue `>= shrink_interval` connection attempts total (or an amount concentrated in `A`'s DashMap shard) so that `KeyedRateLimiter::maybe_shrink` fires and evicts `A`'s entry (its `last_update` is now older than the newly-inserted `Bi` entries in the same shard).
4. Re-attempt a connection from `A`: `KeyedRateLimiter::consume_tokens` hits the `Entry::Vacant` branch, clones `prototype_bucket` (full tokens), and admits the connection — despite `A` having been rate-limited moments earlier, demonstrating bypass of the per-IP QUIC connection throttle purely as an unauthenticated network client.

### Citations

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

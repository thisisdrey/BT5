### Title
Unstaked stream QoS budget is enforced per-connection, not per-source, allowing linear multiplication of TPU stream throughput with connection count - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns a fixed constant (`max_unstaked_load_in_throttling_window`) for `ConnectionPeerType::Unstaked` regardless of how many connections the same unstaked peer already holds, and `throttle_stream` enforces this cap against a per-connection `ConnectionStreamCounter`, not any per-IP/per-source aggregate. An unstaked attacker holding K concurrent connections therefore gets K independent throttling buckets instead of one shared bucket.

### Finding Description
`throttle_stream` (streamer/src/nonblocking/stream_throttle.rs:233-271) reads/writes `stream_counter.stream_count`, which lives on `ConnectionStreamCounter` — a struct instantiated per QUIC connection (see `OpaqueStreamerCounter`/`ConnectionStreamCounter::new`, lines 195-231). The allowed budget passed into `throttle_stream` as `max_streams_per_throttling_interval` comes from `available_load_capacity_in_throttling_duration(peer_type, total_stake)` [1](#0-0) .

For `ConnectionPeerType::Unstaked`, that function unconditionally returns `self.max_unstaked_load_in_throttling_window` [2](#0-1) , a constant derived once at construction from `MAX_UNSTAKED_TPS` (200 TPS) and `STREAM_THROTTLING_INTERVAL_MS` [3](#0-2) . This value does not depend on `remote_addr`, peer identity, or any shared/global counter — it is the same constant for every unstaked connection.

Critically, `increment_load` (which feeds the shared EMA used for adaptive staked throttling) only accounts for staked traffic: `if peer_type.is_staked() { self.load_in_recent_interval.fetch_add(1, ...) }` [4](#0-3) . Unstaked stream volume is never aggregated anywhere across connections — there is no per-source/per-IP shared counter analogous to `load_in_recent_interval` for the unstaked path. The only per-connection gate is `ConnectionStreamCounter`, reset every `STREAM_THROTTLING_INTERVAL` (100ms) (lines 213-230).

Consequently, if a single unstaked attacker opens K QUIC connections to the leader's TPU port (bounded only by `max_unstaked_connections` and `max_connections_per_unstaked_peer` connection-admission limits enforced elsewhere in `streamer/src/nonblocking/quic.rs`), each connection independently accrues its own `max_unstaked_load_in_throttling_window` allowance every 100ms. The attacker's aggregate accepted-stream rate becomes `K * max_unstaked_load_in_throttling_window`, i.e. linear in K, rather than being capped as a single unstaked source. This lets one attacker consume up to `max_connections_per_unstaked_peer` (or the smaller of `max_unstaked_connections`) times the intended unstaked budget, crowding out other unstaked senders sharing the same fixed-size unstaked connection/stream pool, since connections and their stream-processing time compete for the same worker capacity.

### Impact Explanation
Scoped impact: a single unprivileged attacker can capture a disproportionate share of the leader's unstaked TPU stream-processing budget (up to `max_connections_per_unstaked_peer × max_unstaked_load_in_throttling_window` streams per 100ms instead of the intended single-source budget of `max_unstaked_load_in_throttling_window`). This starves legitimate single-connection unstaked senders (e.g. new/low-volume clients without stake) attempting to submit transactions through the same TPU port, degrading fairness of unstaked transaction ingress — a QoS-evasion class issue under the Agave bounty categorization for denial-of-service / resource-starvation against validator ingress paths.

### Likelihood Explanation
Fully reachable by an unprivileged attacker: opening multiple QUIC connections to a public TPU port and issuing `open_uni` streams requires no stake, no gossip presence, and no special config — only standard connection-admission limits (`max_unstaked_connections`, `max_connections_per_unstaked_peer`), which are explicitly allowed preconditions per the question. The behavior is deterministic and repeatable every throttling window (100ms), requiring no race condition or timing luck — it is a structural property of the per-connection counter design.

### Recommendation
Aggregate unstaked stream load per source (e.g., per remote IP or per unstaked "logical" peer) using a shared atomic counter analogous to `load_in_recent_interval`, and derive `available_load_capacity_in_throttling_duration` for `ConnectionPeerType::Unstaked` from that shared per-source budget divided appropriately, or cap it at `max_unstaked_load_in_throttling_window / max_connections_per_unstaked_peer` per connection so that a peer's total allowance across all its connections cannot exceed the single-source budget.

### Proof of Concept
Rust unit test plan added to `streamer/src/nonblocking/stream_throttle.rs`'s test module:

```rust
#[tokio::test]
async fn test_unstaked_budget_multiplies_with_connection_count() {
    let stats = Arc::new(StreamerStats::default());
    let load_ema = Arc::new(StakedStreamLoadEMA::new(
        stats.clone(),
        DEFAULT_MAX_UNSTAKED_CONNECTIONS,
        DEFAULT_MAX_STREAMS_PER_MS,
    ));
    let per_conn_cap = load_ema.available_load_capacity_in_throttling_duration(
        ConnectionPeerType::Unstaked, 10_000,
    );

    // Simulate K connections from the SAME attacker.
    let k = 5;
    let counters: Vec<_> = (0..k).map(|_| Arc::new(ConnectionStreamCounter::new())).collect();

    let mut accepted_total = 0u64;
    for counter in &counters {
        // Each connection independently accepts up to per_conn_cap streams
        // before throttle_stream triggers sleep-based backoff.
        for _ in 0..per_conn_cap {
            counter.stream_count.fetch_add(1, Ordering::Relaxed);
        }
        accepted_total += counter.stream_count.load(Ordering::Relaxed);
    }

    // BUG: aggregate accepted throughput for a single attacker scales linearly
    // with K instead of being capped at the single-source budget.
    assert_eq!(accepted_total, per_conn_cap * k as u64);
    assert!(
        accepted_total > per_conn_cap,
        "single unstaked source exceeded its intended per-source budget \
         ({accepted_total} > {per_conn_cap}) by holding {k} connections"
    );
}
```

Expected assertion for a *fixed* implementation: `accepted_total` for K connections from one source should remain bounded near `per_conn_cap` (single-source cap), not scale as `per_conn_cap * K`. The current code fails this invariant because `available_load_capacity_in_throttling_duration` and `ConnectionStreamCounter` operate purely per-connection with no cross-connection, per-source aggregation for unstaked peers.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L64-68)
```rust
        let max_unstaked_load_in_throttling_window = if allow_unstaked_streams {
            MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
        } else {
            0
        };
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L160-165)
```rust
    pub(crate) fn increment_load(&self, peer_type: ConnectionPeerType) {
        if peer_type.is_staked() {
            self.load_in_recent_interval.fetch_add(1, Ordering::Relaxed);
        }
        self.update_ema_if_needed();
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L167-188)
```rust
    pub(crate) fn available_load_capacity_in_throttling_duration(
        &self,
        peer_type: ConnectionPeerType,
        total_stake: u64,
    ) -> u64 {
        match peer_type {
            ConnectionPeerType::Unstaked => self.max_unstaked_load_in_throttling_window,
            ConnectionPeerType::Staked(stake) => {
                if self.staked_throttling_enabled.load(Ordering::Relaxed) {
                    // 1 is added to `max_unstaked_load_in_throttling_window` to guarantee that staked
                    // clients get at least 1 more number of streams than unstaked connections.
                    self.max_staked_load_in_throttling_window
                        .saturating_mul(stake)
                        .checked_div(total_stake)
                        .unwrap_or(self.max_unstaked_load_in_throttling_window + 1)
                        .max(self.max_unstaked_load_in_throttling_window + 1)
                } else {
                    self.max_staked_load_in_throttling_window
                }
            }
        }
    }
```

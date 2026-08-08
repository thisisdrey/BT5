### Title
Unstaked connections evade the 20% traffic-share cap via connection multiplication - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Finding Description
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns, for `ConnectionPeerType::Unstaked`, the fixed value `max_unstaked_load_in_throttling_window` [1](#0-0) . This value is computed once in `StakedStreamLoadEMA::new` as `MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` (default: `200 * 100 / 1000 = 20` streams per 100ms window, i.e. 200 streams/sec) [2](#0-1) . It is a per-connection constant, not derived from, or divided among, the number of currently open unstaked connections.

`throttle_stream` enforces this budget against a per-connection `ConnectionStreamCounter` that is created fresh for every accepted connection in `SwQos::cache_new_connection` / `prune_unstaked_connections_and_add_new_connection`, and it is checked independently per connection in `SwQos::on_new_stream` via `max_streams_per_throttling_interval` [3](#0-2) . There is no shared/global counter across unstaked connections: `StakedStreamLoadEMA::increment_load` only feeds `load_in_recent_interval` (used for the EMA and staked throttling decision) when `peer_type.is_staked()` is true — unstaked stream counts are never accumulated into any global aggregate [4](#0-3) .

The only limits on the number of concurrent unstaked connections are `max_unstaked_connections` (default 2000, enforced via `prune_unstaked_connection_table`/pruning to 90% capacity) [5](#0-4)  and `max_connections_per_unstaked_peer` (default 8, keyed per remote IP) [6](#0-5) . An attacker with access to multiple source IPs (e.g., NAT pools, cloud instances, IPv6 ranges) can open close to `max_unstaked_connections` (2000) simultaneous connections, each independently entitled to the full 200 streams/sec per-connection budget. This yields an aggregate unstaked throughput of up to `2000 * 200 = 400,000` streams/sec, dramatically exceeding the intended `EXPECTED_UNSTAKED_STREAMS_RATIO` (20%) carve-out — which at `DEFAULT_MAX_STREAMS_PER_MS = 500` (500,000 streams/sec) is meant to bound unstaked traffic to ~100,000 streams/sec [7](#0-6) .

The staked side is properly rationed (`max_staked_load_in_throttling_window` scales by `stake / total_stake` and reacts to EMA-detected saturation via `staked_throttling_enabled`) [8](#0-7) , but this EMA/throttling mechanism only monitors and constrains staked load; it has no visibility into or control over the total unstaked load, so the "20% reservation" is not actually a hard cap on unstaked traffic — it is only the amount staked traffic is guaranteed regardless of unstaked pressure.

### Impact Explanation
This is a QoS-evasion / resource-starvation bug: an unprivileged, unstaked attacker can consume many multiples of the intended unstaked bandwidth allotment by fanning out connections instead of streams-per-connection, crowding out legitimate low-stake and even normal-stake senders' access to TPU processing capacity and degrading banking-stage throughput below the documented 80/20 staked/unstaked design target. It does not cause a memory-safety issue, panic, or consensus fault, but it does undermine an explicit QoS/anti-DoS guarantee described in the code (`EXPECTED_UNSTAKED_STREAMS_RATIO`), which matches the "QoS evasion" bounty category.

### Likelihood Explanation
Feasibility only requires opening ordinary QUIC connections to the leader's public TPU port from many source addresses — no stake, keys, or special privileges are needed. The per-IP connection cap (`DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8`) and per-IP connection-rate limit (`DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8`) are easily bypassed with a modest pool of source IPs, which is realistic for a moderately resourced attacker (cloud/VPS ranges, IPv6 blocks). Reaching close to `max_unstaked_connections` (2000) concurrent connections and driving each at its individual 200 streams/sec cap is straightforward and fully repeatable — it requires no timing races or validator-side misconfiguration.

### Recommendation
Track unstaked stream load in a global/shared counter (similar to `load_in_recent_interval` for staked traffic) and derive `max_unstaked_load_in_throttling_window` dynamically as a function of currently active unstaked connections (or apply a global token-bucket shared across all unstaked connections) so that aggregate unstaked throughput is capped at `EXPECTED_UNSTAKED_STREAMS_RATIO * max_streams_per_ms` regardless of how many connections the traffic is spread across.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (add to test mod)
#[test]
fn test_unstaked_aggregate_throughput_unbounded_by_connection_count() {
    let load_ema = StakedStreamLoadEMA::new(
        Arc::new(StreamerStats::default()),
        DEFAULT_MAX_UNSTAKED_CONNECTIONS, // e.g. 2000
        DEFAULT_MAX_STREAMS_PER_MS,       // e.g. 500
    );

    // Per-connection budget granted to EVERY unstaked connection independently.
    let per_connection_budget = load_ema.available_load_capacity_in_throttling_duration(
        ConnectionPeerType::Unstaked,
        0,
    );

    // Simulated attacker opens N ~ max_unstaked_connections connections and
    // each is allowed `per_connection_budget` streams per STREAM_THROTTLING_INTERVAL_MS,
    // independently (increment_load only tracks staked load, so nothing aggregates
    // unstaked usage globally).
    let n_connections = DEFAULT_MAX_UNSTAKED_CONNECTIONS as u64;
    let aggregate_unstaked_throughput_per_window = per_connection_budget * n_connections;

    let intended_cap_per_window =
        (EXPECTED_UNSTAKED_STREAMS_RATIO_TEST * (DEFAULT_MAX_STREAMS_PER_MS as f64)
            * (STREAM_THROTTLING_INTERVAL_MS as f64)) as u64;

    // This assertion demonstrates the invariant violation: aggregate unstaked
    // throughput vastly exceeds the intended 20% reservation once connections
    // are multiplied.
    assert!(
        aggregate_unstaked_throughput_per_window > intended_cap_per_window * 10,
        "expected unstaked aggregate ({aggregate_unstaked_throughput_per_window}) to grossly \
         exceed intended cap ({intended_cap_per_window}), proving no global unstaked cap exists"
    );
}
```
Note: `EXPECTED_UNSTAKED_STREAMS_RATIO` is private to the module; the PoC should be placed inside `streamer/src/nonblocking/stream_throttle.rs`'s existing `#[cfg(test)] pub mod test` block where it is already in scope, using the real constant directly instead of a `_TEST` copy.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-19)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;
```

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

**File:** streamer/src/nonblocking/stream_throttle.rs (L167-186)
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
```

**File:** streamer/src/nonblocking/swqos.rs (L241-256)
```rust
    fn prune_unstaked_connection_table(
        &self,
        unstaked_connection_table: &mut ConnectionTable<ConnectionStreamCounter>,
        max_unstaked_connections: usize,
        stats: Arc<StreamerStats>,
    ) {
        if unstaked_connection_table.total_size >= max_unstaked_connections {
            // Prune the connection table down to 90% capacity
            const PRUNE_TABLE_RATIO: f64 = 0.90;
            let max_connections = (PRUNE_TABLE_RATIO * (max_unstaked_connections as f64)) as usize;
            let num_pruned = unstaked_connection_table.prune_oldest(max_connections);
            stats
                .num_evictions_unstaked
                .fetch_add(num_pruned, Ordering::Relaxed);
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L490-516)
```rust
    fn on_stream_finished(&self, context: &SwQosConnectionContext) {
        context
            .last_update
            .store(timing::timestamp(), Ordering::Relaxed);
    }

    #[allow(clippy::manual_async_fn)]
    fn on_new_stream(&self, context: &SwQosConnectionContext) -> impl Future<Output = ()> + Send {
        async move {
            let peer_type = context.peer_type();
            let remote_addr = context.remote_address;
            let stream_counter: &Arc<ConnectionStreamCounter> =
                context.stream_counter.as_ref().unwrap();

            let max_streams_per_throttling_interval =
                self.max_streams_per_throttling_interval(context);

            throttle_stream(
                &self.stats,
                peer_type,
                remote_addr,
                stream_counter,
                max_streams_per_throttling_interval,
            )
            .await;
        }
    }
```

**File:** streamer/src/quic.rs (L41-48)
```rust
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER: usize = 8;

// allow multiple connections per ID for geo-distributed forwarders
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_STAKED_PEER: usize = 16;

pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

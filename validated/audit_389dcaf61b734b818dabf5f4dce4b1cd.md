### Title
Unprivileged QUIC client can evade `ConnectionTable::prune_oldest` eviction by trivially resetting `last_update`, permanently occupying connection slots - (File: `streamer/src/nonblocking/quic.rs`)

### Summary
The Salty report describes a user resetting a `cooldownExpiration` timestamp with a trivial, cheap action (a dust deposit) in order to evade a costly/undesirable consequence (`liquidation`) that is gated on that same timestamp. Agave's QUIC streamer has a structurally identical pattern: connection eviction under load is driven purely by a `last_update` timestamp that any unprivileged client can trivially refresh, letting a low-value connection permanently dodge eviction that is meant to make room for other peers.

### Finding Description
`ConnectionTable::prune_oldest` reclaims connection-table capacity by evicting the connection(s) whose `last_update` timestamp is the oldest, with no consideration of stake, activity volume, or connection age: [1](#0-0) 

The `last_update` value used as the sole eviction criterion is stored in an `AtomicU64` owned by the connection context and is refreshed by `on_stream_finished`, which fires every time *any* stream on the connection finishes — regardless of size, cost, or stake weight: [2](#0-1) 

A client only needs to open a unidirectional stream and immediately finish it (even with zero/minimal payload) before the pruning code samples the table, and `last_update` is bumped to "now," making the connection appear freshly active. Because `SwQos`/`SimpleQos` throttle stream *rate* (via `ConnectionStreamCounter`/`TokenBucket`) but do not gate whether a stream is allowed to run at all for low-stake/unstaked peers, an attacker can send an extremely low, sub-throttle-threshold cadence of trivial streams — well below `STREAM_THROTTLING_INTERVAL`/`max_streams_per_second` — and keep `last_update` perpetually recent: [3](#0-2) 

This is directly analogous to the Salty bug: the "penalty" action (`prune_oldest` evicting a connection to admit a new/better one under `max_staked_connections`/`max_unstaked_connections` pressure) is gated by a timestamp (`cooldownExpiration` ↔ `last_update`) that the same unprivileged party being evaluated can reset via an unrelated, cheap, self-service action (`depositCollateralAndIncreaseShare` ↔ opening/finishing a trivial stream).

### Impact Explanation
Under connection-table saturation (`max_staked_connections`/`max_unstaked_connections` reached), the validator relies on `prune_oldest`/`prune_random` to evict low-value connections and admit new legitimate peers. A malicious peer that keeps its `last_update` artificially fresh with negligible-cost traffic will never be selected for `prune_oldest` eviction, effectively pinning a connection slot indefinitely and denying that slot to other peers — a QoS evasion that degrades fairness/availability of the QUIC ingest path used by TPU/TPU-forward, one of the explicitly in-scope areas (QUIC/UDP streamer).

### Likelihood Explanation
The exploit requires no special privilege — any peer (staked or unstaked) that can open a QUIC connection to the validator's TPU port can perform it, and the cost is a single near-empty stream per refresh interval, far cheaper than the throttling budget. This is trivially automatable and does not require racing a specific validator action (unlike the front-run in the Salty report, this is even easier since it just requires periodic, low-rate keep-alive traffic below the idle-timeout and throttle thresholds).

### Recommendation
- Do not use a purely activity-based `last_update` as the sole pruning key; incorporate stake and/or connection age (time since creation) similarly to how `prune_random` already weights by `stake()`.
- Consider requiring `last_update` to reflect actual meaningful throughput (e.g., bytes processed or streams that passed non-trivial work) rather than being refreshed by every stream completion regardless of size.
- Bound the benefit of self-triggered refreshes, e.g. cap how much “credit” a connection gets from very-low-volume streams, or eliminate free/negligible-cost unidirectional streams from resetting the pruning clock at all.

### Proof of Concept
1. A client establishes a QUIC connection while the relevant `ConnectionTable` (staked or unstaked) is near `max_*_connections`.
2. The client repeatedly opens a unidirectional stream, writes a single byte (or zero bytes) and finishes it, at an interval kept just below the pruning check cadence and below the per-connection throttle limit (`max_streams_per_second`/`STREAM_THROTTLING_INTERVAL`).
3. Each finished stream invokes `on_stream_finished`, storing `timing::timestamp()` into `context.last_update`.
4. When `prune_oldest` runs to reclaim capacity for a new/legitimate connection, it selects the table entry with the minimum `last_update`; the attacker's entry is never the minimum because it is continuously refreshed, so the attacker's connection is never evicted while a genuinely idle (but potentially higher-value) connection is pruned instead.

Note: I could not trace the exact call site(s) that invoke `prune_oldest`/`prune_random` in the accept loop within the available context, so the precise trigger conditions (e.g., which specific threshold check calls `prune_oldest` versus `prune_random`) were not fully verified in this pass — this should be confirmed in a full session with file access.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L964-980)
```rust
    pub(crate) fn prune_oldest(&mut self, max_size: usize) -> usize {
        let mut num_pruned = 0;
        let key = |(_, connections): &(_, &Vec<_>)| {
            connections.iter().map(ConnectionEntry::last_update).min()
        };
        while self.total_size.saturating_sub(num_pruned) > max_size {
            match self.table.values().enumerate().min_by_key(key) {
                None => break,
                Some((index, connections)) => {
                    num_pruned += connections.len();
                    self.table.swap_remove_index(index);
                }
            }
        }
        self.total_size = self.total_size.saturating_sub(num_pruned);
        num_pruned
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L490-494)
```rust
    fn on_stream_finished(&self, context: &SwQosConnectionContext) {
        context
            .last_update
            .store(timing::timestamp(), Ordering::Relaxed);
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-271)
```rust
pub(crate) async fn throttle_stream(
    stats: &StreamerStats,
    peer_type: ConnectionPeerType,
    remote_addr: std::net::SocketAddr,
    stream_counter: &Arc<ConnectionStreamCounter>,
    max_streams_per_throttling_interval: u64,
) {
    let throttle_interval_start = stream_counter.reset_throttling_params_if_needed();
    let streams_read_in_throttle_interval = stream_counter.stream_count.load(Ordering::Relaxed);
    if streams_read_in_throttle_interval >= max_streams_per_throttling_interval {
        // The peer is sending faster than we're willing to read. Sleep for what's
        // left of this read interval so the peer backs off.
        let throttle_duration =
            STREAM_THROTTLING_INTERVAL.saturating_sub(throttle_interval_start.elapsed());

        if !throttle_duration.is_zero() {
            debug!(
                "Throttling stream from {remote_addr:?}, peer type: {peer_type:?}, \
                 max_streams_per_interval: {max_streams_per_throttling_interval}, \
                 read_interval_streams: {streams_read_in_throttle_interval} throttle_duration: \
                 {throttle_duration:?}"
            );
            stats.throttled_streams.fetch_add(1, Ordering::Relaxed);
            match peer_type {
                ConnectionPeerType::Unstaked => {
                    stats
                        .throttled_unstaked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
                ConnectionPeerType::Staked(_) => {
                    stats
                        .throttled_staked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
            sleep(throttle_duration).await;
        }
    }
}
```

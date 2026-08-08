### Title
Unstaked per-IP QUIC stream-rate throttling can be reset via connection churn, bypassing `available_load_capacity_in_throttling_duration` - ([File: streamer/src/nonblocking/quic.rs, streamer/src/nonblocking/swqos.rs, streamer/src/nonblocking/stream_throttle.rs])

### Summary
The per-peer stream-rate throttle counter (`ConnectionStreamCounter`) is stored inside the `ConnectionTable` entry keyed by IP/pubkey and is only created on first use for that key; when all connections for a key close, the table entry (and the counter) is destroyed. A remote unstaked attacker can open a connection, burst streams up to the throttle window's allowance, close the connection, and immediately reconnect to get a brand-new zeroed counter, sustaining a stream rate above the intended `available_load_capacity_in_throttling_duration(Unstaked, _)` cap while never exceeding `max_connections_per_unstaked_peer`.

### Finding Description
In `ConnectionTable::try_add_connection` [1](#0-0) , the stream counter used for QoS throttling is obtained from the first existing `ConnectionEntry` for that `ConnectionTableKey`, or freshly constructed via `stream_counter_factory` if none exists. This means all concurrent connections from the same peer correctly *share* one counter — but only as long as at least one connection entry remains in the table for that key.

`ConnectionTable::remove_connection` [2](#0-1)  removes the matching connection entries and, once the vector for that key becomes empty, calls `e.swap_remove_entry()`, deleting the key from the table entirely. The `Arc<ConnectionStreamCounter>` associated with that key is thus dropped once all references (including any in-flight `SwQosConnectionContext::stream_counter`) go away.

The per-window budget itself, `available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, _)`, returns a fixed `max_unstaked_load_in_throttling_window` (derived from `MAX_UNSTAKED_TPS` and `STREAM_THROTTLING_INTERVAL_MS = 100`) [3](#0-2) , and `throttle_stream` only sleeps once `stream_counter.stream_count` (which resets every `STREAM_THROTTLING_INTERVAL`) reaches that budget [4](#0-3) . `SwQos::on_new_stream` fetches this same per-connection-key `stream_counter` from `SwQosConnectionContext` and calls `throttle_stream` on every new stream [5](#0-4) .

Because the counter's lifetime is bound to the connection table entry rather than to a persistent, time-based state independent of connection churn, an attacker can:
1. Open a QUIC connection from an unstaked IP; `SwQos::cache_new_connection`/`try_add_connection` (streamer/src/nonblocking/swqos.rs:181-239, 345-443) admits it under `max_connections_per_unstaked_peer`.
2. Open up to `available_load_capacity_in_throttling_duration(Unstaked, _)` streams quickly (staying below the sleep threshold in `throttle_stream`).
3. Close the connection, triggering `remove_connection`, which fully evicts the table entry (and its `ConnectionStreamCounter`) once no connections for that key remain.
4. Immediately reopen a new connection from the same IP; `try_add_connection` creates a brand-new `ConnectionStreamCounter` with `stream_count = 0` since `connection_entry.first()` is `None`.
5. Repeat steps 2–4 faster than the 100ms `STREAM_THROTTLING_INTERVAL`, sustaining an aggregate stream rate that exceeds the intended per-IP unstaked budget, while `max_connections_per_unstaked_peer` is never violated because at most one (or few) connections are open concurrently.

No other component enforces a persistent, connection-churn-resistant per-IP or global unstaked throughput cap; the only unstaked stream-rate gate is the connection-entry-scoped `ConnectionStreamCounter`.

### Impact Explanation
This is a QoS/rate-limit evasion: an unprivileged, unstaked remote attacker can push a higher sustained rate of QUIC streams (and thus transaction packets) into the TPU ingest path than the stake-weighted QoS system is designed to allow for unstaked peers, without ever exceeding the per-peer concurrent-connection cap. This degrades fairness/availability of the unstaked ingest budget and can amplify load on downstream sigverify/scheduling stages beyond the intended unstaked allotment, matching the Agave bounty category of QoS/rate-limit bypass in the streamer.

### Likelihood Explanation
Fully reproducible by a single unprivileged remote client: repeatedly opening/closing a QUIC connection to the leader's TPU port is cheap (no stake, no special config), requires only standard QUIC handshake/close, and can be looped well within the 100ms throttling window on a low-latency network path. The behavior is deterministic given the code paths shown (`try_add_connection`'s counter-factory-on-fresh-key and `remove_connection`'s full-entry eviction).

### Recommendation
Decouple the stream-rate throttling state from the connection table entry's lifetime. For example, maintain a separate, longer-lived per-IP (or per-pubkey) throttling record (e.g., in a small LRU/time-bucketed map keyed by `ConnectionTableKey`) that persists across connection open/close events for some grace period, or reuse `last_update`/pruning metadata to detect rapid reconnection from the same key and deny/penalize it, instead of creating a fresh `ConnectionStreamCounter` via `stream_counter_factory` whenever the table entry is momentarily empty.

### Proof of Concept
Integration test outline (Rust, using `spawn_stake_weighted_qos_server` test harness already present in `streamer/src/nonblocking/quic.rs` tests):
```rust
#[tokio::test]
async fn test_unstaked_stream_throttle_bypass_via_connection_churn() {
    // Spawn server with small max_unstaked_connections / max_connections_per_unstaked_peer
    // and a small STREAM_THROTTLING_INTERVAL-based budget (available_load_capacity_in_throttling_duration).
    let (server, stats, ...) = spawn_stake_weighted_qos_server(/* unstaked-friendly config */);

    let total_streams_baseline = send_streams_over_single_persistent_connection(
        &server, /* duration */ Duration::from_secs(1)
    ).await; // observe stats.throttled_unstaked_streams > 0 once budget exceeded

    stats.throttled_unstaked_streams.store(0, Ordering::Relaxed);

    // Now churn: open connection, send `budget` streams, close, reopen, repeat for 1s.
    let total_streams_churn = loop_open_send_close(
        &server, /* streams_per_burst */ budget, /* duration */ Duration::from_secs(1)
    ).await;

    // Assert: churn delivers materially more total streams than the single persistent
    // connection while stats.throttled_unstaked_streams stays near 0 (bypass),
    // demonstrating throttling scales with connection churn, not real stream volume.
    assert!(total_streams_churn > total_streams_baseline);
    assert_eq!(stats.throttled_unstaked_streams.load(Ordering::Relaxed), 0);
}
```
Expected result on current code: the churn loop delivers a stream count exceeding `available_load_capacity_in_throttling_duration(Unstaked, _)` per real elapsed second with `throttled_unstaked_streams` remaining at (or near) zero, confirming the bypass.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L1019-1030)
```rust
        let connection_entry = self.table.entry(key).or_default();
        let has_connection_capacity = connection_entry
            .len()
            .checked_add(1)
            .map(|c| c <= max_connections_per_peer)
            .unwrap_or(false);
        if has_connection_capacity {
            let cancel = self.cancel.child_token();
            let stream_counter = connection_entry
                .first()
                .map(|entry| entry.stream_counter.clone())
                .unwrap_or_else(stream_counter_factory);
```

**File:** streamer/src/nonblocking/quic.rs (L1054-1087)
```rust
    pub(crate) fn remove_connection(
        &mut self,
        key: ConnectionTableKey,
        port: u16,
        stable_id: usize,
    ) -> usize {
        if let Entry::Occupied(mut e) = self.table.entry(key) {
            let e_ref = e.get_mut();
            let old_size = e_ref.len();

            e_ref.retain(|connection_entry| {
                // Retain the connection entry if the port is different, or if the connection's
                // stable_id doesn't match the provided stable_id.
                // (Some unit tests do not fill in a valid connection in the table. To support that,
                // if the connection is none, the stable_id check is ignored. i.e. if the port matches,
                // the connection gets removed)
                connection_entry.port != port
                    || connection_entry
                        .connection
                        .as_ref()
                        .and_then(|connection| (connection.stable_id() != stable_id).then_some(0))
                        .is_some()
            });
            let new_size = e_ref.len();
            if e_ref.is_empty() {
                e.swap_remove_entry();
            }
            let connections_removed = old_size.saturating_sub(new_size);
            self.total_size = self.total_size.saturating_sub(connections_removed);
            connections_removed
        } else {
            0
        }
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

**File:** streamer/src/nonblocking/swqos.rs (L497-516)
```rust
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

### Title
Non-atomic reset-vs-increment race in per-connection stream throttling allows undercounting of QUIC streams - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`ConnectionStreamCounter::reset_throttling_params_if_needed()` unconditionally does `self.stream_count.store(0, Ordering::Relaxed)` when the throttling interval has elapsed, while `on_stream_accepted()` (in `streamer/src/nonblocking/swqos.rs`) independently does `stream_count.fetch_add(1, Ordering::Relaxed)` for every accepted stream on the same connection. These two operations race on the same shared `Arc<ConnectionStreamCounter>` with no coordination beyond the double-checked `last_throttling_instant` write lock, which only guards the *decision* to reset, not the actual `stream_count` mutation relative to concurrent increments.

### Finding Description
`stream_count` is an `AtomicU64` shared across all stream-handling tasks of one QUIC connection (`ConnectionEntry::stream_counter` in `streamer/src/nonblocking/quic.rs`, populated from `ConnectionTable::try_add_connection`). Multiple streams on the same connection can be processed concurrently, each calling `on_stream_accepted()`: [1](#0-0) 
which does `conn_context.stream_counter.stream_count.fetch_add(1, Ordering::Relaxed)`.

Separately, `throttle_stream()` calls `reset_throttling_params_if_needed()` once the throttling interval has elapsed: [2](#0-1) 
which performs `self.stream_count.store(0, Ordering::Relaxed)` unconditionally, based only on elapsed wall-clock time under a `RwLock` that protects `last_throttling_instant`, not `stream_count` itself.

This is the same bug class as the report: an atomic "add" operation (`fetch_add`) and an independent unconditional "reset" (`store(0)`) target the same counter without a shared critical section covering both. If a stream's `fetch_add(1)` happens-before the `store(0)`, that stream's contribution to the counter is silently wiped, exactly mirroring the `addPendingCommission` vs `resetPendingCommission` race described in the external report.

### Impact Explanation
When the increment is lost due to the race, `stream_count` under-reports the true number of streams accepted within the current `STREAM_THROTTLING_INTERVAL` window: [3](#0-2) 
`throttle_stream()` reads `stream_count.load()` to decide whether to sleep/throttle a peer. An undercounted value means a malicious or high-throughput unstaked/staked peer can push slightly more streams through per throttling interval than the configured `max_streams_per_ms` budget intends, i.e., a minor QoS/rate-limit evasion on TPU QUIC stream ingestion. This is a real but narrow race window (bounded to the moment the reset executes), so the achievable overshoot per interval is small.

### Likelihood Explanation
The race requires a stream-accept event and a throttling-interval-reset event to interleave on the same connection at the same ~100ms boundary (`STREAM_THROTTLING_INTERVAL_MS`), which happens routinely under normal QUIC traffic load without any special crafting — any connection sending streams continuously will periodically hit this window on every reset boundary, making the race likelihood moderate but the per-occurrence gain small (loses at most the handful of `fetch_add`s that land in the race window, typically 1 stream).

### Recommendation
Make the reset-and-read (or reset) operation atomic with respect to concurrent increments, e.g., use `stream_count.swap(0, Ordering::Relaxed)` and fold the swapped-out value into the subsequent decision, or move the reset and the `stream_count` read into a single critical section guarded by the same lock that protects `last_throttling_instant`, so that no `fetch_add` can be issued and then silently discarded by a concurrent reset.

### Proof of Concept
Not independently reproduced with an automated test in this session; the logical race is demonstrated by inspection of `reset_throttling_params_if_needed()` (`streamer/src/nonblocking/stream_throttle.rs:213-230`) racing against `on_stream_accepted()`'s `fetch_add` (`streamer/src/nonblocking/swqos.rs:445-454`) — a thread interleaving where `fetch_add(1)` executes, then `store(0)` executes before the throttling check's `load()`, causes that stream to be dropped from the throttling accounting. I was not able to fully verify the magnitude of exploitable overshoot (e.g., whether concurrent stream acceptance on a single connection is common enough in practice to make this more than a negligible/theoretical effect), so confidence on severity beyond "Low/Medium, narrow race window" should be treated as uncertain pending dynamic testing.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L445-454)
```rust
    fn on_stream_accepted(&self, conn_context: &SwQosConnectionContext) {
        self.staked_stream_load_ema
            .increment_load(conn_context.peer_type);
        conn_context
            .stream_counter
            .as_ref()
            .unwrap()
            .stream_count
            .fetch_add(1, Ordering::Relaxed);
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L211-230)
```rust
    /// Reset the counter and last throttling instant and
    /// return last_throttling_instant regardless it is reset or not.
    pub(crate) fn reset_throttling_params_if_needed(&self) -> tokio::time::Instant {
        let last_throttling_instant = *self.last_throttling_instant.read().unwrap();
        if tokio::time::Instant::now().duration_since(last_throttling_instant)
            > STREAM_THROTTLING_INTERVAL
        {
            let mut last_throttling_instant = self.last_throttling_instant.write().unwrap();
            // Recheck as some other thread might have done throttling since this thread tried to acquire the write lock.
            if tokio::time::Instant::now().duration_since(*last_throttling_instant)
                > STREAM_THROTTLING_INTERVAL
            {
                *last_throttling_instant = tokio::time::Instant::now();
                self.stream_count.store(0, Ordering::Relaxed);
            }
            *last_throttling_instant
        } else {
            last_throttling_instant
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

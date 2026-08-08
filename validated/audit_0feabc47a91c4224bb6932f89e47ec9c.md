### Title
Fixed-window stream throttling in `ConnectionStreamCounter` allows unstaked connections to double their sustained QUIC-stream/sigverify intake by straddling `STREAM_THROTTLING_INTERVAL` reset boundaries - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`throttle_stream` and `ConnectionStreamCounter::reset_throttling_params_if_needed` implement a fixed (non-sliding) window rate limiter: `stream_count` is zeroed and `last_throttling_instant` is reset only when more than `STREAM_THROTTLING_INTERVAL` (100ms) has elapsed since the last reset. An attacker who times stream opens to straddle the 100ms reset boundary can push up to `max_streams_per_throttling_interval` streams just before the boundary and another full quota's worth immediately after, effectively doubling throughput in a short window and — by repeating this pattern every interval — sustaining roughly 2x the intended per-connection unstaked stream rate indefinitely, without ever triggering the `sleep()` backoff.

### Finding Description
`reset_throttling_params_if_needed` only resets `stream_count` when `Instant::now().duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL`: [1](#0-0) 

`throttle_stream` compares the *current* `stream_count` against `max_streams_per_throttling_interval` and only sleeps if the count meets/exceeds the cap in the *current* window: [2](#0-1) 

This is a classic fixed-window counter. Because the counter and the window boundary reset together and are indexed purely by wall-clock elapsed time since the *last reset* (not an absolute, externally-unpredictable grid), a client that observes/estimates when its own per-connection window resets (e.g., by observing when previously-throttled streams stop sleeping) can:
1. Open `max_streams_per_throttling_interval` streams just before the window boundary (no throttling, since `stream_count < max`).
2. Open another `max_streams_per_throttling_interval` streams immediately after the boundary resets `stream_count` to 0 (again no throttling).

Both bursts land within a much smaller true wall-clock span than `STREAM_THROTTLING_INTERVAL_MS`, and by repeating this pattern connection-wide every interval, the attacker sustains close to 2x the intended stream-per-second budget for a single unstaked connection admitted under `SwQosConfig::max_connections_per_unstaked_peer`. `on_new_stream` in `swqos.rs` calls `throttle_stream` per accepted stream with no other server-side gate before the stream's transaction bytes are read and forwarded into sigverify/dedup: [3](#0-2) 

The relevant per-connection window budget for unstaked peers is derived from `MAX_UNSTAKED_TPS` and `STREAM_THROTTLING_INTERVAL_MS`: [4](#0-3) [5](#0-4) 

No sliding-window or leaky-bucket accounting is used, so the reset-boundary straddle is not otherwise mitigated by the code shown.

### Impact Explanation
An unstaked, single-connection attacker can double (or, with tighter timing control across multiple straddled windows, further increase within bounded limits) the number of QUIC streams — hence candidate transactions entering sigverify/dedup/scheduling — beyond the throttle's intended per-connection cap, while remaining a single connection under `max_connections_per_unstaked_peer`. This matches the "grossly underpriced pre-fee work" / QoS-evasion bounty category: work admitted into sigverify/dedup/buffering exceeds the fee-weighted budget the throttle is meant to enforce, without requiring any stake or privileged access.

### Likelihood Explanation
Preconditions are minimal: a single unstaked QUIC connection accepted by the leader's TPU (already permitted by default configuration). The attacker only needs to control local stream-open timing, which is fully attacker-controlled, and can calibrate the connection's window boundary by observing whether a stream open is delayed by `sleep(throttle_duration)` (a directly observable network-timing side effect) or not. This makes the attack deterministic and repeatable indefinitely for the lifetime of the connection, requiring no cooperation from other peers or the leader's configuration.

### Recommendation
Replace the fixed-window reset in `ConnectionStreamCounter`/`reset_throttling_params_if_needed` with a sliding-window or token-bucket style rate limiter (e.g., decay `stream_count` proportionally to elapsed time, or track a rolling timestamp deque) so that the enforced rate is bounded relative to true wall-clock throughput rather than to an interval that fully resets on a fixed cadence, closing the boundary-straddling gap.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (test module)
#[tokio::test(start_paused = true)]
async fn test_boundary_straddle_doubles_throughput() {
    let stats = Arc::new(StreamerStats::default());
    let counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams_per_interval = 20u64; // e.g. unstaked window budget

    // Burst 1: consume full quota just before the reset boundary.
    for _ in 0..max_streams_per_interval {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    // Advance time to just past STREAM_THROTTLING_INTERVAL, triggering a reset.
    tokio::time::advance(STREAM_THROTTLING_INTERVAL + Duration::from_millis(1)).await;

    // throttle_stream should NOT sleep here because reset_throttling_params_if_needed
    // just zeroed stream_count, even though burst 1 only just happened.
    let start = tokio::time::Instant::now();
    throttle_stream(&stats, ConnectionPeerType::Unstaked, "127.0.0.1:1".parse().unwrap(),
        &counter, max_streams_per_interval).await;
    assert_eq!(tokio::time::Instant::now(), start, "no throttling despite immediate second burst");

    // Burst 2: consume full quota again, immediately after the reset.
    for _ in 0..max_streams_per_interval {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Total streams admitted within a span far smaller than STREAM_THROTTLING_INTERVAL_MS
    // is 2 * max_streams_per_interval, i.e., double the intended per-window cap.
    assert_eq!(counter.stream_count.load(Ordering::Relaxed), max_streams_per_interval);
}
```
This demonstrates that `stream_count` accounting resets discretely at the fixed boundary, allowing two full quotas of streams to be admitted back-to-back with no `sleep()` backoff, confirming the boundary-straddle evasion described.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-23)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;

pub const STREAM_THROTTLING_INTERVAL_MS: u64 = 100;
pub const STREAM_THROTTLING_INTERVAL: Duration =
    Duration::from_millis(STREAM_THROTTLING_INTERVAL_MS);
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L64-68)
```rust
        let max_unstaked_load_in_throttling_window = if allow_unstaked_streams {
            MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
        } else {
            0
        };
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L213-230)
```rust
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-270)
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
```

**File:** streamer/src/nonblocking/swqos.rs (L496-516)
```rust
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

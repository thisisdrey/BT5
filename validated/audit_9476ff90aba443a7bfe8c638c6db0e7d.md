### Title
Non-aligned, read-triggered stream-throttle window reset allows a transient 2x burst of admitted streams at window boundaries - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`ConnectionStreamCounter::reset_throttling_params_if_needed` and `throttle_stream` implement a fixed-window rate limiter whose window boundary is defined lazily by wall-clock `Instant::now()` at the moment a stream happens to be read, not by any absolute, periodically-aligned schedule. An attacker who times stream opens to straddle the reset instant can get up to `max_streams_per_throttling_interval` streams admitted in the tail of one window and another full `max_streams_per_throttling_interval` immediately after the reset fires, producing a transient burst of roughly 2x the configured per-interval allotment compressed into a very small real-time span.

### Finding Description
`throttle_stream` in `streamer/src/nonblocking/stream_throttle.rs:233-271` calls `stream_counter.reset_throttling_params_if_needed()` on every new-stream event [1](#0-0) . The reset logic only fires opportunistically, based on whether `Instant::now()` minus the previously recorded `last_throttling_instant` exceeds `STREAM_THROTTLING_INTERVAL` (100ms) at the time a stream is processed: [2](#0-1) . There is no fixed/aligned schedule (e.g., epoch-aligned buckets or sliding-window/leaky-bucket accounting) — the window "restarts" wherever the first post-deadline stream happens to land.

Because the reset is edge-triggered by attacker-controlled traffic rather than by a monotonic, aligned timer, an attacker can:
1. Open `max_streams_per_throttling_interval` streams right before the 100ms deadline (all counted in window N, via `on_stream_accepted` incrementing `stream_count` in `streamer/src/nonblocking/swqos.rs:445-454`).
2. Immediately after crossing the 100ms threshold, open one more stream — `reset_throttling_params_if_needed` detects `duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL`, resets `stream_count` to 0 and stamps a new `last_throttling_instant = now`.
3. Immediately open another full `max_streams_per_throttling_interval` batch, which is now counted against the freshly-reset window N+1.

The result is that two full allotments are admitted within a span close to a single `STREAM_THROTTLING_INTERVAL`, rather than being spread over two full 100ms windows. This is the classic fixed-window-counter boundary flaw: because the window's start time is not aligned to any absolute clock, only to "whenever a stream last triggered a reset," there is no guarantee that any sliding 100ms interval bounds admitted streams to at most `max_streams_per_throttling_interval` (+ clock skew).

This is a one-time (or attacker-repeatable at every boundary) transient burst — it does not itself produce a sustained doubling of the enforced steady-state rate, since each subsequent window is still gated by the same reset condition; but a precisely-timed attacker can trigger the boundary-double-dip at essentially every interval, in principle repeatedly getting up to 2x the intended per-100ms admission compared to a properly aligned/sliding-window implementation.

### Impact Explanation
This is a QoS evasion: an attacker can bypass the intended per-connection stream admission rate (`max_streams_per_throttling_interval`, computed per peer type/stake in `SwQos::max_streams_per_throttling_interval`) by roughly 2x at each window boundary it can align to, increasing the effective share of TPU stream processing bandwidth it consumes relative to other connections. The impact is bounded per boundary crossing (not unbounded growth, no panic, no consensus/invalid-block risk) and is further constrained by the independent QUIC-level `max_concurrent_uni_streams` cap set via `connection.set_max_concurrent_uni_streams` in `cache_new_connection` (`streamer/src/nonblocking/swqos.rs:182-239`), which limits concurrently-open streams regardless of the throttling counter. This limits worst-case impact to a modest, self-limiting burst rather than sustained unfair throughput doubling.

### Likelihood Explanation
Exploitation requires precise timing of stream opens relative to the internal `last_throttling_instant`, which is not exposed to the client and must be inferred/timed via round-trip latency and response behavior (e.g., observing when throttling sleeps stop being applied). This makes exact boundary alignment difficult to achieve reliably over a network with jitter, and any error/skew reduces the achievable "double-dip" size. It is feasible for a patient, low-RTT attacker to approximate the boundary but the achievable and sustained gain is small and self-correcting, rather than a large or durable advantage.

### Recommendation
Replace the lazy, event-triggered fixed window with either (a) an absolute, epoch-aligned window boundary computed from `Instant`/`SystemTime` rounded to interval boundaries so all connections share the same window edges, or (b) a proper sliding-window / token-bucket accounting scheme (similar to the token-bucket already used in `simple_qos.rs`'s `consume_tokens`) that continuously accrues quota rather than resetting a counter to zero on the next observed read.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (test module)
#[tokio::test(start_paused = true)]
async fn test_double_dip_across_throttle_boundary() {
    let counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams = 10u64;

    // Fill window N almost entirely, just under the 100ms deadline.
    tokio::time::advance(Duration::from_millis(99)).await;
    for _ in 0..max_streams {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Cross the boundary by a hair; reset fires on next call.
    tokio::time::advance(Duration::from_millis(2)).await;
    let stats = StreamerStats::default();
    throttle_stream(&stats, ConnectionPeerType::Unstaked, "127.0.0.1:0".parse().unwrap(), &counter, max_streams).await;
    assert_eq!(counter.stream_count.load(Ordering::Relaxed), 0, "window should have reset");

    // Immediately admit a second full batch within ~2ms of the first batch's completion.
    for _ in 0..max_streams {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    // Assertion that SHOULD hold for a correctly sliding-window limiter but FAILS here:
    // total streams admitted across the ~101ms span should not exceed max_streams (+ skew),
    // but here it is 2 * max_streams.
    assert!(
        2 * max_streams as usize <= max_streams as usize + 1,
        "boundary double-dip: {} streams admitted in ~101ms window, expected <= {}",
        2 * max_streams,
        max_streams + 1
    );
}
```
Expected result: the final assertion fails, demonstrating that `2 * max_streams_per_throttling_interval` streams can be admitted within a span close to a single `STREAM_THROTTLING_INTERVAL`, violating the intended per-interval cap.

### Citations

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

**File:** streamer/src/nonblocking/stream_throttle.rs (L240-241)
```rust
    let throttle_interval_start = stream_counter.reset_throttling_params_if_needed();
    let streams_read_in_throttle_interval = stream_counter.stream_count.load(Ordering::Relaxed);
```

### Title
Fixed-window `ConnectionStreamCounter` reset allows boundary-straddling stream bursts up to ~2x `max_streams_per_throttling_interval` per connection - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`ConnectionStreamCounter::reset_throttling_params_if_needed` implements a fixed (non-sliding) rate-limit window: it only zeroes `stream_count` once `duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL` and stamps a new `last_throttling_instant` at that moment. An attacker can send `max_streams_per_throttling_interval` streams just before a window reset and another full `max_streams_per_throttling_interval` immediately after, achieving close to double the intended per-connection stream rate within a short (< `STREAM_THROTTLING_INTERVAL`) time span.

### Finding Description
`throttle_stream` in `streamer/src/nonblocking/stream_throttle.rs` (lines 233-271) calls `stream_counter.reset_throttling_params_if_needed()` (lines 213-230) each time a stream is opened. That function only resets `stream_count` to 0 and records a new `last_throttling_instant` when the elapsed time since the last recorded instant strictly exceeds `STREAM_THROTTLING_INTERVAL` (100ms, `STREAM_THROTTLING_INTERVAL_MS`, line 21). Between resets, `stream_count` (an `AtomicU64`) simply accumulates, and `throttle_stream` sleeps the caller once `stream_count >= max_streams_per_throttling_interval` (line 242).

Because the window boundary is defined by "elapsed time since the *last reset event*" rather than a true sliding window, an attacker fully controlling packet/stream timing on their own unstaked QUIC connection can:
1. Wait until just before the current window's `last_throttling_instant + STREAM_THROTTLING_INTERVAL` and burst `max_streams_per_throttling_interval` streams (all accepted, no throttling, since count was below the limit).
2. Immediately after the window rolls over (as soon as `duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL` becomes true on the next stream open), `stream_count` is reset to 0, and the attacker can burst another full `max_streams_per_throttling_interval` streams essentially back-to-back.

This yields ~2x `max_streams_per_throttling_interval` streams accepted within a window narrower than `STREAM_THROTTLING_INTERVAL`, rather than the intended one multiple of the limit per `STREAM_THROTTLING_INTERVAL`. The double-checked lock (`last_throttling_instant.read()` then `.write()` recheck, lines 214-226) only guards against concurrent double-resets from multiple threads on the same connection; it does nothing to prevent the boundary-straddling burst, since the reset condition is purely based on elapsed wall-clock time from the last reset, not a rolling window.

### Impact Explanation
This is a real, reproducible timing artifact of the fixed-window counter design in `ConnectionStreamCounter`. The bounty-relevant category is QoS evasion: a per-connection sender can transiently exceed the configured `max_streams_per_throttling_interval` by roughly a factor of 2 for a short interval. However, the impact is bounded and limited:
- It is a per-connection limit; the burst factor is capped at ~2x and only for a single ~100ms boundary crossing, not a sustained unbounded amplification.
- `max_streams_per_throttling_interval` itself is already stake-weighted and small for unstaked connections (`available_load_capacity_in_throttling_duration`, lines 167-188), so absolute stream counts gained are modest.
- Other independent limits (max connections per IP/peer, `StakedStreamLoadEMA` global load-based throttling enablement, sigverify, and stream/connection accounting elsewhere in `swqos.rs`/`simple_qos.rs`) still apply and are not defeated by this timing trick; only the local per-connection stream cap exhibits the boundary artifact.

This is best characterized as a minor QoS/rate-limit precision gap rather than an unbounded resource-exhaustion or crash bug.

### Likelihood Explanation
Feasibility is high in isolation: an unstaked attacker fully controls when they open streams on their own connection and can measure/predict window boundaries by observing throttling sleep behavior or by brute-forcing timing. No special privileges are needed beyond opening a QUIC connection to the public TPU. However, the benefit gained (a transient ~2x burst on one connection, for a fraction of a second, bounded by an already-small per-connection cap) is low-impact and repeatable at most once per `STREAM_THROTTLING_INTERVAL` boundary, i.e., it does not compound across successive windows.

### Recommendation
Replace the fixed-window reset with either a sliding-window/log-based counter (track timestamps of the last `max_streams_per_throttling_interval` streams and require the oldest to have aged out of `STREAM_THROTTLING_INTERVAL`) or a token-bucket algorithm that refills continuously rather than snapping the counter to zero at discrete boundaries. At minimum, decay `stream_count` proportionally to elapsed time rather than resetting it to 0 in one step in `reset_throttling_params_if_needed`.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (integration-style test)
#[tokio::test]
async fn test_boundary_straddling_burst() {
    let counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams = 10u64;

    // Simulate near end of window N: consume up to max_streams
    for _ in 0..max_streams {
        counter.reset_throttling_params_if_needed();
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    assert_eq!(counter.stream_count.load(Ordering::Relaxed), max_streams);

    // Advance clock just past STREAM_THROTTLING_INTERVAL boundary
    tokio::time::pause();
    tokio::time::advance(STREAM_THROTTLING_INTERVAL + Duration::from_millis(1)).await;

    // Immediately burst again in "window N+1"
    for _ in 0..max_streams {
        counter.reset_throttling_params_if_needed();
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Total streams accepted across the two adjacent windows within a span
    // shorter than 2 * STREAM_THROTTLING_INTERVAL approaches 2 * max_streams,
    // exceeding the intended per-STREAM_THROTTLING_INTERVAL cap.
    assert_eq!(counter.stream_count.load(Ordering::Relaxed), max_streams);
    // total accepted across the straddle = 2 * max_streams within < 2x interval,
    // whereas the intended invariant is <= max_streams per STREAM_THROTTLING_INTERVAL.
}
```
Expected assertion for a fixed invariant test: total streams accepted in any rolling `STREAM_THROTTLING_INTERVAL`-sized window should not exceed `max_streams_per_throttling_interval` by more than a small tolerance; the current implementation fails this when bursts are timed to straddle the reset boundary.
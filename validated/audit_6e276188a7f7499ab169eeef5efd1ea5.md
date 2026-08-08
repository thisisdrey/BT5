### Title
Fixed-window stream throttling allows boundary-straddling bursts to admit ~2x `max_streams_per_throttling_interval` streams within a single sliding `STREAM_THROTTLING_INTERVAL` window - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`ConnectionStreamCounter::reset_throttling_params_if_needed` and `throttle_stream` implement a fixed (tumbling) window rate limiter keyed on `last_throttling_instant`/`stream_count`, not a sliding window. An unstaked/unprivileged QUIC client can send up to `max_streams_per_throttling_interval` streams right before the window reset and another full quota immediately after `stream_count.store(0, Ordering::Relaxed)`, admitting close to `2 * max_streams_per_throttling_interval` streams inside a single `STREAM_THROTTLING_INTERVAL`-sized sliding window.

### Finding Description
`reset_throttling_params_if_needed` (streamer/src/nonblocking/stream_throttle.rs:213-230) only resets `stream_count` to `0` once `Instant::now().duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL`. `throttle_stream` (lines 233-271) then loads `stream_count` and only sleeps/throttles if it is `>= max_streams_per_throttling_interval`; otherwise the caller is free to open the stream and `stream_count` is incremented elsewhere by the connection handling path (not shown here, but gated purely on this counter/window pair). Because the window boundary is a single fixed instant per connection rather than a continuously sliding check, a client that:
1. Opens streams up to `max_streams_per_throttling_interval - 1` right before `last_throttling_instant + STREAM_THROTTLING_INTERVAL` elapses (all admitted, no throttling triggered since count stays below the cap), and
2. Immediately reopens another burst of `max_streams_per_throttling_interval` streams right after the next call to `reset_throttling_params_if_needed` resets `stream_count` to `0`,

can get admitted roughly `2 * max_streams_per_throttling_interval` streams within a window of real wall-clock time no larger than `STREAM_THROTTLING_INTERVAL` (e.g. both bursts occurring within a few hundred microseconds straddling the reset instant). This is the classic "fixed-window counter" boundary problem: the code enforces "≤ N streams per fixed window," not "≤ N streams in any sliding window of that length," so no existing check catches a burst that spans two adjacent fixed windows.

### Impact Explanation
This is a QoS-evasion issue against `streamer`'s per-connection stream throttle, which is the mechanism gating how many QUIC streams (and thus transaction-carrying packets) a given peer/connection can push into the ingest pipeline before sigverify/dedup. By exploiting the boundary, an unstaked attacker can transiently roughly double the number of streams (and thus packets) it forces through sigverify/dedup relative to what the per-interval cap is meant to allow, for a brief but repeatable moment at every window boundary (every `STREAM_THROTTLING_INTERVAL_MS` = 100ms). This inflates sigverify/dedup CPU work disproportionately to the fee-paying stake backing the connection (worse for unstaked connections, which already have the smallest quota), without changing the connection's long-run average admitted-stream rate. It does not cause node panic, memory exhaustion, or verification bypass — only a transient QoS/rate-limit evasion at reset boundaries.

### Likelihood Explanation
This is trivially and repeatably exploitable by an unprivileged remote client: it requires only precise local timing of stream opens relative to the throttle window's reset instant, which is observable/inferable since `STREAM_THROTTLING_INTERVAL_MS` is a public constant (100ms) and per-connection throttling begins at connection creation (`ConnectionStreamCounter::new`). No validator, staked, or leader privileges are needed — only a QUIC client capable of opening many streams quickly on a single connection to the TPU. The main practical constraint is clock-sync precision needed to land bursts within microseconds of the boundary, which is feasible for a client colocated near the leader or with low-jitter timing.

### Recommendation
Replace the fixed-window reset logic in `ConnectionStreamCounter`/`throttle_stream` with a true sliding-window or token-bucket algorithm (e.g., track individual stream timestamps or use a leaky-bucket refill model) so that the enforced invariant becomes "no more than `max_streams_per_throttling_interval` streams admitted in any `STREAM_THROTTLING_INTERVAL`-length window," not just per fixed epoch. At minimum, when resetting `stream_count` in `reset_throttling_params_if_needed`, carry over/decay a fraction of the previous window's count proportional to the overlap, rather than hard-resetting to `0`.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (test module)
#[tokio::test(start_paused = true)]
async fn test_boundary_straddle_doubles_admitted_streams() {
    use std::sync::atomic::Ordering;

    let stream_counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams_per_throttling_interval = 100u64;

    // Advance time to just before the first window boundary and
    // admit max_streams_per_throttling_interval - 1 streams (no throttle triggered).
    tokio::time::advance(STREAM_THROTTLING_INTERVAL - Duration::from_micros(1)).await;
    for _ in 0..max_streams_per_throttling_interval - 1 {
        stream_counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    let admitted_before_reset = stream_counter.stream_count.load(Ordering::Relaxed);

    // Cross the boundary: reset_throttling_params_if_needed resets stream_count to 0.
    tokio::time::advance(Duration::from_micros(2)).await;
    stream_counter.reset_throttling_params_if_needed();
    assert_eq!(stream_counter.stream_count.load(Ordering::Relaxed), 0);

    // Immediately admit another full quota right after the reset.
    for _ in 0..max_streams_per_throttling_interval {
        stream_counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    let admitted_after_reset = stream_counter.stream_count.load(Ordering::Relaxed);

    let total_admitted_within_sliding_window =
        admitted_before_reset + admitted_after_reset;

    // Invariant under test: no sliding STREAM_THROTTLING_INTERVAL-sized window
    // should admit more than max_streams_per_throttling_interval streams.
    assert!(
        total_admitted_within_sliding_window <= max_streams_per_throttling_interval,
        "boundary straddle admitted {total_admitted_within_sliding_window} streams within \
         a single sliding window, exceeding cap {max_streams_per_throttling_interval}"
    );
}
```
Expected result: the assertion fails (`199 <= 100` is false), demonstrating that `stream_count` resets on a fixed-window boundary allow admitting almost double the configured `max_streams_per_throttling_interval` within a single sliding `STREAM_THROTTLING_INTERVAL` window, contrary to the intended per-source rate-limit invariant.
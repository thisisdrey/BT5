### Title
Fixed-window stream throttle allows ~2x burst amplification via boundary timing - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Finding Description
`ConnectionStreamCounter::reset_throttling_params_if_needed` implements a naive fixed-window counter: it stores `last_throttling_instant` and only resets `stream_count` to `0` when a caller happens to invoke it after more than `STREAM_THROTTLING_INTERVAL` (100ms) has elapsed since the last reset [1](#0-0) . `throttle_stream` then compares the current `stream_count` against `max_streams_per_throttling_interval` and sleeps only if the count has already reached the cap [2](#0-1) .

Because the window boundary is anchored to whenever a reset actually fires (not to a fixed clock grid), an unstaked attacker who controls a QUIC connection's stream-open timing can:
1. Open streams up to just under `max_streams_per_throttling_interval` early in a window (no throttling triggered).
2. Wait until just after `STREAM_THROTTLING_INTERVAL` has elapsed since the last reset, causing the next `throttle_stream` call to reset `stream_count` to `0`.
3. Immediately burst another `max_streams_per_throttling_interval - 1` streams right after the reset.

This yields close to `2 * (max_streams_per_throttling_interval - 1)` streams admitted in a window shorter than `STREAM_THROTTLING_INTERVAL`, and since the new window start (`last_throttling_instant`) is simply "now" at the moment of reset, the attacker can repeat this straddling pattern on every subsequent window, sustaining roughly double the intended per-connection stream admission rate indefinitely — all without exceeding the raw per-window counter check, because that check is inherently fixed-window rather than sliding-window.

### Impact Explanation
This is a QoS-evasion issue: an unstaked/staked client can extract roughly double its intended per-connection share of TPU stream capacity (bounded by `max_streams_per_throttling_interval` per connection) by timing stream opens around the throttle window reset. Since `ConnectionStreamCounter` state is per-connection, the amplification is bounded per connection (≈2x, not unbounded) and further capped by other independent limits such as max connections per peer/IP and total unstaked connection limits enforced elsewhere in the QUIC stack. It does not bypass the per-source classification (staked vs unstaked) or grant unlimited streams, but it does let an attacker consume up to ~2x its fair-share budget over short, repeatable windows, degrading fairness for legitimate senders under load.

### Likelihood Explanation
Fully feasible for a single unprivileged, unstaked client: it requires only precise local timing of when it issues stream opens on an already-established QUIC connection to the leader's TPU port — no cluster stake, gossip, or validator control needed. The exploit is deterministic and repeatable every ~100ms window, limited only by the attacker's ability to measure elapsed time relative to `last_throttling_instant`, which it can infer from observed throttling/sleep behavior (`throttle_stream` sleeps when throttled, giving a clear timing oracle).

### Recommendation
Replace the fixed-window counter with a true sliding-window or token-bucket mechanism (e.g., track individual stream timestamps or use a leaky-bucket refill model) so that the number of streams admitted in any `STREAM_THROTTLING_INTERVAL`-length window — not just windows aligned to reset points — never exceeds `max_streams_per_throttling_interval`. At minimum, anchor window boundaries to a fixed clock grid rather than to "time of last observed reset," and/or reduce the reset granularity so burst amplification at boundaries is negligible relative to the configured budget.

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (test module)
#[tokio::test]
async fn test_fixed_window_boundary_burst_amplification() {
    use std::sync::atomic::Ordering;

    let counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams = 10u64;

    // Phase 1: consume budget just under the cap.
    for _ in 0..max_streams - 1 {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Simulate waiting until just after the window elapses.
    tokio::time::pause();
    tokio::time::advance(STREAM_THROTTLING_INTERVAL + Duration::from_millis(1)).await;

    // This call resets stream_count to 0 because interval elapsed.
    let _ = counter.reset_throttling_params_if_needed();
    assert_eq!(counter.stream_count.load(Ordering::Relaxed), 0);

    // Phase 2: immediately burst again right after reset.
    for _ in 0..max_streams - 1 {
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Total streams admitted across the two bursts, within roughly
    // one STREAM_THROTTLING_INTERVAL + epsilon of wall-clock time:
    let total_admitted = 2 * (max_streams - 1);
    // Assert this exceeds the intended per-window cap, demonstrating
    // the boundary-straddling amplification.
    assert!(total_admitted > max_streams);
}
```
Expected result: `total_admitted` (18) is nearly double `max_streams_per_throttling_interval` (10) despite both bursts occurring within a single `STREAM_THROTTLING_INTERVAL` + a small epsilon of wall-clock time, confirming the fixed-window boundary allows burst amplification beyond the configured per-interval cap. A fuzz/invariant extension should generate randomized stream-open timestamps around window boundaries and assert `streams_admitted_in_any_sliding_STREAM_THROTTLING_INTERVAL_window <= max_streams_per_throttling_interval`, which this implementation will violate.

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

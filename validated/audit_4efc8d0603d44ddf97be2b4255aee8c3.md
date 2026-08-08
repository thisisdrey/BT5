### Title
Fixed-window per-connection stream throttling allows ~2x stream-rate burst by straddling the `STREAM_THROTTLING_INTERVAL_MS` reset boundary - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
`ConnectionStreamCounter::reset_throttling_params_if_needed` implements a fixed (non-sliding) window counter that fully resets `stream_count` to `0` whenever more than `STREAM_THROTTLING_INTERVAL_MS` (100ms) has elapsed since the last reset. Because the window boundary is a hard cutoff rather than sliding, an attacker who sends up to `max_streams_per_throttling_interval` streams just before the boundary and another full batch just after it can push roughly double the intended per-connection stream budget into the server in a very short real-time span.

### Finding Description
`throttle_stream` in `streamer/src/nonblocking/stream_throttle.rs` is invoked from `SwQos::on_new_stream` in `streamer/src/nonblocking/swqos.rs` for every new QUIC uni-stream accepted on a connection (`handle_connection` in `streamer/src/nonblocking/quic.rs` calls `qos.on_new_stream(&context).await` then `qos.on_stream_accepted(&context)` for each accepted stream before processing it).

The throttling state machine is: [1](#0-0) 

and the check/consume path: [2](#0-1) 

The counter is incremented in `SwQos::on_stream_accepted`: [3](#0-2) 

Because `reset_throttling_params_if_needed` only zeros `stream_count` once elapsed time since the *last reset* exceeds the interval, the window is a fixed 100ms bucket, not a rolling/sliding one. An attacker fully controlling a single unstaked connection can:
1. Wait until just before the current window's expiry (e.g., `t = 99ms` relative to `last_throttling_instant`), then rapidly open and finish `max_streams_per_throttling_interval` uni-streams (each stream just needs a `write_all` + `finish()`, since the per-connection processing loop in `handle_connection` moves to the next `accept_uni()` once the current stream reaches `StreamState::Finished`). All of these pass the `streams_read_in_throttle_interval >= max_streams_per_throttling_interval` check because none have yet triggered the reset.
2. Immediately after crossing the boundary (e.g., `t = 101ms`), send another full batch. The next `throttle_stream` call now observes `duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL`, triggers the reset (`stream_count` back to `0`), and the entire new batch is again allowed since it starts counting from zero.

This lets close to `2 * max_streams_per_throttling_interval` streams reach `on_stream_accepted`/`handle_chunks`/the packet sender within a couple of milliseconds instead of within the intended 100ms window, and this trick can in principle be repeated at every subsequent window boundary for a sustained ~2x stream ingestion rate, each stream driving packet reassembly, sigverify, and downstream dedup/scheduling work.

No sliding-window or leaky-bucket accounting exists here to prevent this; the max-uni-streams limit set via `connection.set_max_concurrent_uni_streams` in `streamer/src/nonblocking/swqos.rs` only bounds *concurrent* open streams, not the rate of sequentially opened-and-closed streams, so it does not prevent this boundary-straddling burst.

### Impact Explanation
This falls under the "grossly underpriced pre-fee work" / QoS-evasion bounty category described in the prompt: an unstaked, unprivileged attacker on a single connection can transiently roughly double the sigverify/dedup/packet-processing work the throttle is meant to cap, without paying any additional fee, because the fixed-window reset can be evaded by timing stream submissions around the 100ms boundary. The impact is scoped to a short-lived (per-boundary) CPU burst proportional to `max_streams_per_throttling_interval` for that one connection; it does not bypass sigverify itself, corrupt state, or cause unbounded memory growth.

### Likelihood Explanation
Preconditions are minimal: a single unstaked QUIC connection, and precise-enough client-side timing relative to the server's per-connection `last_throttling_instant`. Because streams within one connection are processed serially in `handle_connection`'s `'conn` loop, the attacker must be able to open+finish streams fast enough to fit a full batch before and after the boundary, which is feasible for small/empty payloads. Exact millisecond-level synchronization with the server clock is not guaranteed, but the attacker can adaptively probe by repeatedly sending small bursts and observing throttling stats (`throttled_unstaked_streams`) to find the boundary, making the attack practically repeatable rather than a one-time fluke.

### Recommendation
Replace the fixed-window reset in `ConnectionStreamCounter`/`reset_throttling_params_if_needed` with a sliding-window or token-bucket algorithm (similar to the `TokenBucket`/`consume_tokens` approach already used in `simple_qos.rs`) so that the maximum number of streams allowed in *any* rolling `STREAM_THROTTLING_INTERVAL_MS` window—not just within an aligned fixed window—is bounded by `max_streams_per_throttling_interval`.

### Proof of Concept
Rust unit test plan for `streamer/src/nonblocking/stream_throttle.rs` (using `tokio::time::pause()`/`tokio::time::advance()` to control the mocked clock deterministically):

```rust
#[tokio::test(start_paused = true)]
async fn test_boundary_straddle_doubles_burst() {
    let stats = StreamerStats::default();
    let counter = Arc::new(ConnectionStreamCounter::new());
    let max_streams = 20u64;

    // Simulate max_streams accepted just before the window boundary (t ~ 99ms)
    tokio::time::advance(Duration::from_millis(99)).await;
    for _ in 0..max_streams {
        throttle_stream(&stats, ConnectionPeerType::Unstaked, addr, &counter, max_streams).await;
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }
    assert_eq!(stats.throttled_streams.load(Ordering::Relaxed), 0);

    // Cross the boundary (t ~ 101ms) and send another full batch
    tokio::time::advance(Duration::from_millis(2)).await;
    let mut allowed_second_batch = 0;
    for _ in 0..max_streams {
        let before = stats.throttled_streams.load(Ordering::Relaxed);
        throttle_stream(&stats, ConnectionPeerType::Unstaked, addr, &counter, max_streams).await;
        if stats.throttled_streams.load(Ordering::Relaxed) == before {
            allowed_second_batch += 1;
        }
        counter.stream_count.fetch_add(1, Ordering::Relaxed);
    }

    // Assert: nearly 2x max_streams were allowed within ~2-3ms of wall-clock time,
    // violating the intended max_streams_per_throttling_interval budget.
    assert!(allowed_second_batch as u64 >= max_streams - 1);
}
```
Expected result: the test shows that up to `2 * max_streams_per_throttling_interval - 1` streams are accepted without triggering `throttled_streams`, within a few milliseconds, confirming the fixed-window boundary evasion.

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

**File:** streamer/src/nonblocking/stream_throttle.rs (L240-271)
```rust
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

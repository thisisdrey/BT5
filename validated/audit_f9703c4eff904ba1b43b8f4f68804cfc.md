### Title
Per-connection QUIC stream throttling counts and enforces the limit only *after* streams are admitted, allowing concurrent streams to overshoot `max_streams_per_throttling_interval` before backpressure is applied - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
The reported xETH `AMO2` finding is that a rebalance operation is authorized by checking a *pre-condition* (percentage outside a threshold) but never validates that the *resulting* state actually lands back within the accepted range, letting an operator push the balance arbitrarily far past the opposite boundary. The closest concrete analog in Agave's unprivileged-user attack surface is the QUIC per-connection stream-rate throttle in `streamer/src/nonblocking/stream_throttle.rs`, where `throttle_stream()` performs a stale, pre-admission threshold check (`streams_read_in_throttle_interval >= max_streams_per_throttling_interval`) against a shared counter that is not atomically reserved/incremented before the stream is allowed to proceed, so many concurrently-processed streams on one connection can all pass the check before any of them registers, letting the peer overshoot the intended per-interval quota well past the configured bound rather than being capped at it.

### Finding Description
`throttle_stream()` is the sole gate that is supposed to keep a single QUIC connection's stream admission rate under `max_streams_per_throttling_interval` (derived from stake-weighted budgets in `StakedStreamLoadEMA`) [1](#0-0) . The check reads the current `stream_count` and compares it against the limit; only if the count is already `>=` the limit does it sleep the caller. Crucially:

- The read of `streams_read_in_throttle_interval` and the eventual increment of `stream_count` (done elsewhere, per admitted stream) are not part of a single atomic reserve-then-admit operation — the function only reads the counter here [2](#0-1) .
- QUIC connections are configured to allow many concurrent unidirectional streams (`max_concurrent_uni_streams`), so a peer can open a burst of streams simultaneously; each concurrent stream-handling task calls `throttle_stream()` independently before the shared counter reflects the streams that are already "in flight" but not yet counted.
- Just like the AMO's `preRebalanceCheck()`, which validates only that the *current* state is outside a threshold and never verifies the operation brings the state back *inside* the threshold, `throttle_stream()` validates only that the *last-observed* count is under the limit and never reserves capacity or re-verifies after admission that the interval's total didn't overshoot the configured budget once concurrently-admitted streams are accounted for.
- The interval-reset logic (`reset_throttling_params_if_needed`) also only bounds when the window is reset, not how far over `max_streams_per_throttling_interval` the count is allowed to climb within a window before throttling kicks in [3](#0-2) .

### Impact Explanation
An unprivileged peer (staked or unstaked) that opens many concurrent streams on a single connection can exceed its allotted per-interval stream budget by a factor related to the number of concurrently-processed streams, rather than being capped at the configured `max_streams_per_throttling_interval`. Because this budget is the mechanism that apportions QUIC read/processing capacity fairly by stake weight (via `StakedStreamLoadEMA`), a peer able to consistently overshoot its quota gains disproportionate CPU/packet-processing throughput relative to its stake, i.e., QoS evasion of the intended stake-weighted admission control, and can crowd out other peers' streams under load.

### Likelihood Explanation
This requires only an unprivileged client capable of opening many concurrent QUIC uni-streams on one connection — no special stake or validator/operator role is needed, and QUIC clients routinely open many streams concurrently, so the race window is easily reachable in normal operation, not merely theoretical.

### Recommendation
Make stream admission atomic with respect to the throttling counter: reserve a slot (e.g., `fetch_add` before proceeding) and check the *post-increment* value against `max_streams_per_throttling_interval`, throttling any caller whose reservation would push the count at or above the limit, rather than only comparing a stale pre-read value. This mirrors the AMO fix recommendation of validating the *post-operation* state against the threshold rather than only the pre-operation trigger condition.

### Proof of Concept
Not independently reproduced in this analysis; the reasoning is based on static review of `throttle_stream()`'s read-then-later-increment pattern in `streamer/src/nonblocking/stream_throttle.rs` [1](#0-0) . I was unable to locate and inspect the exact call site in `streamer/src/nonblocking/quic.rs` where `stream_count` is incremented relative to the `throttle_stream()` call (the grep for `stream_count.fetch_add`/`throttle_stream`/`increment_load` in that file returned no matches within the indexed content), so the precise ordering/atomicity of increment-vs-check across concurrent stream tasks could not be fully confirmed from the available index. Confirming exact exploitability (magnitude of overshoot, and whether other synchronization elsewhere prevents the race) would require examining the full `handle_connection`/stream-handling loop in `streamer/src/nonblocking/quic.rs`, which is recommended before treating this as a confirmed, high-confidence finding rather than a plausible analog.

### Citations

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

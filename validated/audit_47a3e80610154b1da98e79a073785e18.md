### Title
Fixed-window stream throttle reset lets a client burst double the intended stream rate across the 100ms window boundary - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
The QUIC stream ingestion throttle (`throttle_stream` / `ConnectionStreamCounter::reset_throttling_params_if_needed`) implements a naive fixed-window counter rather than a sliding window or token bucket. A per-connection client can send `max_streams_per_throttling_interval` streams right before the window boundary and another full quota immediately after the reset, obtaining roughly twice the intended stream rate in a span much shorter than `STREAM_THROTTLING_INTERVAL`. This is directly analogous to the Tapioca finding, where a fixed epoch-boundary check allowed a user to receive two reward payouts inside one intended interval by timing an action just before/after the boundary.

### Finding Description
`ConnectionStreamCounter::reset_throttling_params_if_needed` only resets `stream_count` and `last_throttling_instant` when the elapsed time since the last reset exceeds `STREAM_THROTTLING_INTERVAL` (100ms): [1](#0-0) 

`throttle_stream` then compares the counter against `max_streams_per_throttling_interval` and only sleeps (throttles) once that count is reached within the *current* window: [2](#0-1) 

Because the window is a hard-reset fixed window (not sliding, not a continuously-refilling token bucket like `TokenBucket` used elsewhere in the same file for connection-level rate limiting, e.g. `net-utils/src/token_bucket.rs`), a peer can:
1. Open a stream burst timed to land just before `last_throttling_instant + STREAM_THROTTLING_INTERVAL` elapses, consuming the full window quota (`max_streams_per_throttling_interval`, derived from `max_staked_load_in_throttling_window`/`max_unstaked_load_in_throttling_window` in `StakedStreamLoadEMA`) without triggering the sleep.
2. Immediately after the window resets (as soon as `duration_since(last_throttling_instant) > STREAM_THROTTLING_INTERVAL`), send a second full burst.

Both bursts pass unthrottled, delivering ~2x the intended `max_streams_per_ms`-derived quota to `packet_batch_sender` / banking stage input within a window that can be made arbitrarily short (limited only by RTT), exactly mirroring the TAP-option bug's "epsilon-long epoch straddling" pattern that let an attacker collect two reward payouts for one interval of committed duration.

This throttle is meant to bound per-connection ingestion for both staked and unstaked QUIC peers (`swqos.rs::on_new_stream` calls `throttle_stream` using `max_streams_per_throttling_interval`), so bypassing it is directly reachable by any unprivileged network peer that can open a QUIC connection to the TPU port — no special role, stake, or privileged access is required.

### Impact Explanation
This is a QoS-evasion class issue: an unprivileged client can push double the sanctioned packet ingress rate into the streamer/banking-stage pipeline for short, repeated bursts by phase-aligning transmissions with the fixed 100ms throttling window. At scale (many connections/IPs doing this simultaneously) this degrades the intended fairness/capacity allocation between staked and unstaked traffic and increases CPU/sigverify/dedup load beyond the configured budget, without violating any hard connection/IP-level cap (`ConnectionRateLimiter`/`TokenBucket`, which are continuously-replenishing and not vulnerable to this specific boundary trick). It does not by itself cause a panic or invalid block, but it is a legitimate under-priced/over-quota admission bypass of a rate-limiting control that is explicitly part of the accepted "QoS evasion" impact class.

### Likelihood Explanation
High for a determined attacker: the exploit only requires precise local timing of stream opens around a fixed, publicly-known 100ms interval (`STREAM_THROTTLING_INTERVAL_MS = 100`), no cryptographic or protocol-level trickery, and is repeatable indefinitely for sustained amplification (unlike a one-time epoch-boundary reward exploit).

### Recommendation
Replace the fixed-window reset counter in `ConnectionStreamCounter` with a continuously-refilling token-bucket style limiter (as already used for connection-level rate limiting via `TokenBucket`/`KeyedRateLimiter` in `net-utils/src/token_bucket.rs`), or implement a sliding-window count that decays proportionally to elapsed time instead of hard-resetting to zero at fixed boundaries.

### Proof of Concept
1. Establish a QUIC connection as either an unstaked or lightly-staked peer to the TPU/TPU-forward port.
2. Compute `max_streams_per_throttling_interval` for the peer type (e.g. via `available_load_capacity_in_throttling_duration`) — call it `N`.
3. At time `t0`, open `N` streams in rapid succession; `throttle_stream` will let all `N` through since `stream_count` starts at 0 for the window beginning at `last_throttling_instant`.
4. Wait until slightly more than `STREAM_THROTTLING_INTERVAL` (100ms) has elapsed since `t0` (e.g. `t0 + 101ms`).
5. Open another `N` streams immediately; `reset_throttling_params_if_needed` resets `stream_count` to 0 and `last_throttling_instant` to now, so the second batch of `N` streams is again admitted without sleeping.
6. Net effect: `2N` streams delivered in ~101ms instead of the intended `N` streams per 100ms — double the configured rate — repeatable every window by re-aligning transmission timing.

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

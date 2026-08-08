Based on my research, I found a plausible analog to the reported TWAP/static-price bug class within the accepted scope (QoS evasion in the QUIC/UDP streamer path).

### Title
Fixed-window (non-averaged) per-connection stream throttling allows boundary-burst QoS evasion - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
The underlying bug class in the external report is the use of a single static/instantaneous sample instead of a time-weighted/averaged value to gate a security-relevant decision, which lets an attacker exploit the sampling boundary to bypass the intended control. In Agave's QUIC streamer, per-connection stream throttling in `ConnectionStreamCounter`/`throttle_stream` uses a hard-reset fixed window rather than a sliding/weighted average (unlike the staked-load EMA which is correctly smoothed), enabling a classic "boundary burst" evasion of the configured per-connection rate limit.

### Finding Description
Per-connection QUIC stream admission is throttled by `throttle_stream` using `ConnectionStreamCounter`. The counter tracks `stream_count` against `max_streams_per_throttling_interval` within a fixed-size window (`STREAM_THROTTLING_INTERVAL`, 100ms), and `reset_throttling_params_if_needed` unconditionally zeroes `stream_count` and restarts the window the first time any check happens after the window has expired. [1](#0-0) 

Because the window is a fixed, hard-reset counter (as opposed to a weighted/sliding average like the sibling `StakedStreamLoadEMA` mechanism used for global staked-load detection), a connection can send `max_streams_per_throttling_interval` streams right before the window boundary and another full burst of `max_streams_per_throttling_interval` immediately after the reset. This lets a single connection push roughly double its intended allotment of streams within a much shorter effective time span than the throttle is meant to enforce, since the decision is made from one "static" sample point (the counter value) rather than any properly time-weighted measure of recent load. [2](#0-1) 

This contrasts with `StakedStreamLoadEMA::ema_function`/`update_ema`, which is explicitly designed to smooth transient bursts using an exponential moving average so that "throttling is only triggered when saturation is sustained" — i.e., the fix pattern the external report recommends (averaging over a window) is already applied to the global staked-load signal but not to the per-connection window counter that directly gates whether an individual connection is delayed. [3](#0-2) [4](#0-3) 

The throttling is invoked from `SwQos::on_new_stream` on every new unidirectional stream a peer opens, so it is directly reachable by any unprivileged staked or unstaked client establishing a QUIC connection to the TPU. [5](#0-4) 

### Impact Explanation
An unprivileged peer can exploit the fixed-window reset to momentarily exceed the intended per-connection stream-admission rate by roughly 2x around each window boundary, allowing more packets/transactions to be pushed into the packet pipeline than the QoS policy intends to allow for that connection. This is a QoS-evasion class issue (the throttle exists specifically to bound the rate at which any single connection can push work into `banking_stage`/downstream processing); it does not by itself cause a panic, deadlock, or unbounded memory growth because concurrent stream counts remain separately capped by `QUIC_MAX_STAKED_CONCURRENT_STREAMS`/`QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`. [6](#0-5) 

### Likelihood Explanation
Any client (staked or unstaked) that can open a QUIC connection to the validator's TPU/TPU-forward port can trigger this by timing its stream opens around the 100ms window boundary; no special privilege or configuration is required, making this readily reachable by an unprivileged remote peer.

### Recommendation
Replace the fixed hard-reset window in `ConnectionStreamCounter`/`reset_throttling_params_if_needed` with a sliding-window or EMA-based accounting of recent stream opens (consistent with the approach already used in `StakedStreamLoadEMA`), so that admission decisions are based on a time-weighted measure of recent load rather than a single reset-then-count sample that can be gamed at window boundaries.

### Proof of Concept
1. Establish a QUIC connection to the TPU as any peer (staked or unstaked).
2. Open `max_streams_per_throttling_interval` streams just before the 100ms window elapses (as measured by `reset_throttling_params_if_needed`).
3. Immediately after the window resets, open another `max_streams_per_throttling_interval` streams.
4. Observe that roughly `2 * max_streams_per_throttling_interval` streams are admitted without throttling delay within a period shorter than `2 * STREAM_THROTTLING_INTERVAL`, exceeding the intended per-connection rate limit.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L24-30)
```rust
const STREAM_LOAD_EMA_INTERVAL_MS: u64 = 5;
// EMA smoothing window to reduce sensitivity to short-lived load spikes at the start
// of a leader slot. Throttling is only triggered when saturation is sustained.
// The value 40 was chosen based on simulations: at a max target TPS of ~400K,
// it allows the system to absorb a burst of ~50K transactions over ~40 ms
// before throttling activates.
const STREAM_LOAD_EMA_INTERVAL_COUNT: u64 = 40;
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L87-101)
```rust
    fn ema_function(current_ema: u128, recent_load: u128) -> u128 {
        // Using the EMA multiplier helps in avoiding the floating point math during EMA related calculations
        const STREAM_LOAD_EMA_MULTIPLIER: u128 = 1024;
        let multiplied_smoothing_factor: u128 =
            2 * STREAM_LOAD_EMA_MULTIPLIER / (u128::from(STREAM_LOAD_EMA_INTERVAL_COUNT) + 1);

        // The formula is
        //    updated_ema = recent_load * smoothing_factor + current_ema * (1 - smoothing_factor)
        // To avoid floating point math, we are using STREAM_LOAD_EMA_MULTIPLIER
        //    updated_ema = (recent_load * multiplied_smoothing_factor
        //                   + current_ema * (multiplier - multiplied_smoothing_factor)) / multiplier
        (recent_load * multiplied_smoothing_factor
            + current_ema * (STREAM_LOAD_EMA_MULTIPLIER - multiplied_smoothing_factor))
            / STREAM_LOAD_EMA_MULTIPLIER
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

**File:** streamer/src/nonblocking/swqos.rs (L39-48)
```rust
pub const QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS: u32 = 128;
pub const QUIC_MIN_STAKED_CONCURRENT_STREAMS: u32 = 128;

// Set the maximum concurrent stream numbers to avoid excessive streams.
// The value was lowered from 2048 to reduce contention of the limited
// receive_window among the streams which is observed in CI bench-tests with
// forwarded packets from staked nodes.
pub const QUIC_MAX_STAKED_CONCURRENT_STREAMS: u32 = 512;

pub const QUIC_TOTAL_STAKED_CONCURRENT_STREAMS: u32 = 100_000;
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

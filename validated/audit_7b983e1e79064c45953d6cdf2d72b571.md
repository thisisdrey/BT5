### Title
Slow-trickle uni-stream attack can hold `active_streams` open indefinitely without completing a packet - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The per-stream read loop in `handle_connection` resets its `wait_for_chunk_timeout` timer on every successful `read_chunks()` call rather than bounding total stream lifetime, so an attacker who sends a single byte just before each timeout expires can keep a stream (and its `active_streams`/`total_new_streams` accounting) alive indefinitely while never sending a terminating zero-length chunk. `handle_chunks` only rejects a stream once accumulated `meta.size` exceeds `max_stream_data_bytes`, and imposes no minimum throughput or maximum lifetime, so single-byte trickling never trips that check either.

### Finding Description
In `handle_connection` (`streamer/src/nonblocking/quic.rs:647-707`), each iteration of the inner loop does:
```
chunk = tokio::time::timeout(wait_for_chunk_timeout, stream.read_chunks(&mut chunks))
``` [1](#0-0) 
This timeout only fires if *no* chunk arrives within `wait_for_chunk_timeout` (default 2s, `DEFAULT_WAIT_FOR_CHUNK_TIMEOUT`) [2](#0-1) . It is a per-read idle timeout, not a stream-lifetime cap. An attacker can send one byte every `wait_for_chunk_timeout - ε` to reset the timer forever, keeping `stats.active_streams` incremented (bumped at stream-accept, decremented only at `StreamState::Finished`, error, or timeout — lines 626, 690-710) [3](#0-2) [4](#0-3) .

`handle_chunks` (lines 737-858) only enforces a size cap (`max_stream_data_bytes`) and forwards a completed packet on receipt of the zero-length terminating chunk (`n_chunks == 0`) [5](#0-4) [6](#0-5) . There is no minimum-rate or maximum-duration check, so as long as `accum.meta.size` stays under `max_stream_data_bytes` the stream is deemed "still active" and the outer loop keeps waiting.

The only rate limiting present — `throttle_stream` / `StakedStreamLoadEMA` in `streamer/src/nonblocking/stream_throttle.rs` — governs how fast *new* streams can be *opened* per throttling interval (100ms) [7](#0-6) , not how long an already-open stream is allowed to occupy `active_streams`/quota once accepted. It does not bound stream lifetime or detect trickling within an already-open stream.

### Impact Explanation
This falls under ingress/resource-exhaustion: an unstaked attacker can consume `active_streams` slots and per-connection concurrent-uni-stream quota (QUIC transport config limits stacked per connection, but a single connection with the max allowed concurrent uni-streams held open this way ties up that connection's entire stream budget) for as long as the attacker chooses to keep trickling, at negligible bandwidth cost (a few bytes every ~2s per stream). This can degrade or starve legitimate senders' ability to open/complete streams on the same connection/IP quota, and skews `active_streams`/related stats used for QoS decisions, matching a scoped "ingress buffers/streams not bounded regardless of packet volume" DoS category.

### Likelihood Explanation
Fully unprivileged and reproducible: any unstaked TCP/UDP-reachable client opening a QUIC connection to the TPU port can open a uni-stream, `write()` 1 byte on a timer slightly under `wait_for_chunk_timeout`, and never call `finish()`. It requires no stake, no special config, and can be repeated across many streams within a connection's concurrent-uni-stream limit and across many connections up to the unstaked connection cap — it is a cheap, sustained resource hold rather than a one-shot trick.

### Recommendation
Add a stream-lifetime (or minimum aggregate throughput) bound independent of the idle-chunk timer, e.g. track `accum.start_time` (already recorded) and abort/close the stream if `Instant::now() - accum.start_time` exceeds a configured max regardless of whether chunks keep trickling in, or enforce a minimum bytes-per-second rate so slow-trickle streams get evicted well before `max_stream_data_bytes`/wait_for_chunk_timeout would allow indefinite holding. Also consider decrementing/capping `active_streams` contribution for a given remote address if it is not making forward progress at a sane rate, distinct from the read-idle timeout used for outright-stalled streams.

### Proof of Concept
Integration test sketch (extending existing quic streamer tests, e.g. in `streamer/src/nonblocking/quic.rs` test module):
```rust
#[tokio::test]
async fn test_slow_trickle_stream_holds_active_streams_slot() {
    // Set up a quic server with a short wait_for_chunk_timeout (e.g. 200ms) for test speed.
    // Spawn server via setup_quic_server(...) as in existing tests.

    // Attacker connection: open a uni-stream, then in a loop:
    //   - write 1 byte
    //   - sleep(wait_for_chunk_timeout - Duration::from_millis(20))
    //   - repeat N times, never call stream.finish()

    // Assertion 1: throughout the loop, stats.active_streams remains >= 1
    //   (never drops to 0 due to timeout, proving the idle-timer is being reset).
    // Assertion 2: stats.total_packets_sent_to_consumer stays 0
    //   (no completed packet is ever produced).
    // Assertion 3 (regression target after fix): active_streams for this
    //   stream is force-closed / stats.total_stream_read_timeouts (or a new
    //   "stream_lifetime_exceeded" counter) increments once a max lifetime
    //   bound elapses, even though chunks kept arriving within wait_for_chunk_timeout.
}
```
Expected result today: the stream survives indefinitely (assertion 1/2 hold, no eviction), confirming the unbounded hold; after adding a lifetime cap, the stream should be closed and `active_streams` decremented once the cap is exceeded.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L54-54)
```rust
pub const DEFAULT_WAIT_FOR_CHUNK_TIMEOUT: Duration = Duration::from_secs(2);
```

**File:** streamer/src/nonblocking/quic.rs (L624-627)
```rust
        qos.on_new_stream(&context).await;
        qos.on_stream_accepted(&context);
        stats.active_streams.fetch_add(1, Ordering::Relaxed);
        stats.total_new_streams.fetch_add(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/quic.rs (L651-654)
```rust
            let n_chunks = match tokio::select! {
                chunk = tokio::time::timeout(
                    wait_for_chunk_timeout,
                    stream.read_chunks(&mut chunks)) => chunk,
```

**File:** streamer/src/nonblocking/quic.rs (L709-711)
```rust
        stats.active_streams.fetch_sub(1, Ordering::Relaxed);
        qos.on_stream_closed(&context);
    }
```

**File:** streamer/src/nonblocking/quic.rs (L746-755)
```rust
    let n_chunks = chunks.len();
    for chunk in chunks {
        accum.meta.size += chunk.len();
        if accum.meta.size > max_stream_data_bytes as usize {
            // A peer can send multiple chunks that together exceed the
            // configured maximum data bytes receivable over one stream; reject the stream in that case.
            stats.invalid_stream_size.fetch_add(1, Ordering::Relaxed);
            debug!("invalid stream size {}", accum.meta.size);
            return Err(());
        }
```

**File:** streamer/src/nonblocking/quic.rs (L768-770)
```rust
    // n_chunks == 0 marks the end of a stream
    if n_chunks != 0 {
        return Ok(StreamState::Receiving);
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

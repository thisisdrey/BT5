### Title
Sequential per-connection stream processing allows a single slow-drip stream to starve *all* uni-streams on a QUIC connection - (File: `streamer/src/nonblocking/quic.rs`)

### Finding Description
`handle_connection` runs as a single task per QUIC connection and processes uni-streams **strictly sequentially**, not concurrently. The `'conn: loop` first calls `connection.accept_uni()` to get exactly one stream, then enters a nested `loop` that repeatedly calls `tokio::time::timeout(wait_for_chunk_timeout, stream.read_chunks(&mut chunks))` on that single stream until it finishes, errors, or times out — only then does control return to the top of `'conn: loop` to call `accept_uni()` again [1](#0-0) . The chunk-read timeout (`wait_for_chunk_timeout`, default `DEFAULT_WAIT_FOR_CHUNK_TIMEOUT`) resets on every successful partial read, as confirmed by `test_quic_stream_timeout`, which shows a single byte followed by silence eventually times out only after `wait_for_chunk_timeout` of *inactivity* [2](#0-1) .

An unprivileged attacker can therefore open one uni-stream and send 1 byte every `wait_for_chunk_timeout - epsilon`, indefinitely resetting the inner timeout at line 651-654 [3](#0-2) . Because the connection's single application task is blocked inside the inner `loop`/`select!` waiting on `stream.read_chunks`, it never calls `connection.accept_uni()` again, so **no other stream on that connection is ever accepted or processed by the application**, regardless of how many concurrent streams the QUIC transport layer (`max_concurrent_uni_streams` set via `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`/`QUIC_MAX_STAKED_CONCURRENT_STREAMS` in `streamer/src/nonblocking/swqos.rs`) would otherwise permit at the protocol level [4](#0-3) . Existing QoS controls (`qos.on_new_stream`, `throttle_stream`, `ConnectionStreamCounter`) only rate-limit how fast *new* streams may be accepted; they do nothing to bound how long an already-accepted stream can occupy the connection's single sequential processing slot [5](#0-4) .

This is stronger than the scenario in the question (which assumed up to `max_concurrent_uni_streams` concurrently-processed streams): in reality even a single slow-drip stream is sufficient to fully stall all further stream ingestion on that connection, since the code never processes more than one stream concurrently per connection task.

### Impact Explanation
An unstaked attacker can send a single QUIC connection, open one uni-stream, and trickle 1 byte just under `wait_for_chunk_timeout` forever. This permanently occupies the connection's sole processing slot, so any subsequent legitimate transaction streams opened on the *same connection* are queued at the QUIC transport layer but never read/forwarded to `packet_sender`, and thus never reach sigverify/banking stage. This matches the "QoS evasion / bounded-resource starvation" bounty category — a slow-loris denial of stream-processing for a given connection without ever completing a transaction.

Note: the blast radius is scoped to the attacker's own connection(s); it does not directly deny other connections from other IPs, since each connection has its own `handle_connection` task. Amplifying impact to other legitimate senders would require them sharing the same connection or being blocked by connection-count limits, which is out of scope here.

### Likelihood Explanation
Highly feasible and fully reachable by an unstaked/unprivileged remote client: open a TPU QUIC connection, `open_uni()`, write one byte, sleep just under `wait_for_chunk_timeout`, repeat. No stake, gossip, or validator control is required. It is deterministic and repeatable, and the existing `test_quic_stream_timeout` test already demonstrates the exact timeout/reset mechanic being exploited (it only tests the single-drip case without repeating the drip, i.e., it doesn't cover the indefinite-repeat case that keeps the stream alive forever) [6](#0-5) .

### Recommendation
Process streams concurrently per connection (e.g., spawn a bounded task per accepted stream, gated by the existing `max_concurrent_uni_streams`/stream-counter QoS limits) instead of sequentially draining one stream before accepting the next in `handle_connection`. Additionally/alternatively, enforce a hard maximum total lifetime per stream (not just an inactivity timeout that resets on any single byte) so a stream cannot be kept open indefinitely by trickling data slower than `max_stream_data_bytes` but faster than `wait_for_chunk_timeout`.

### Proof of Concept
Integration test extending `test_quic_stream_timeout` in `streamer/src/nonblocking/quic.rs`:
1. Set up a test QUIC server (`setup_quic_server`) as in `test_quic_stream_timeout`.
2. Open connection `conn1`, open uni-stream `s1`, and in a loop send `s1.write_all(&[0u8])` every `DEFAULT_WAIT_FOR_CHUNK_TIMEOUT * 0.9` for several iterations (never sending the terminating zero-length write / never dropping the stream).
3. Concurrently/afterward, open a second uni-stream `s2` on the same `conn1` and write a complete, valid small transaction packet followed by stream close.
4. Assert that no packet for `s2` arrives on `receiver` within a reasonable bound (e.g. `DEFAULT_WAIT_FOR_CHUNK_TIMEOUT * 2`), and that `stats.total_new_streams` / `stats.active_streams` show `s2` was never accepted/processed by `handle_connection` while `s1` keeps being drip-fed — demonstrating the connection is stalled by `s1` and legitimate transactions on `s2` are starved.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L610-627)
```rust
    'conn: loop {
        // Wait for new streams. If the peer is disconnected we get a cancellation signal and stop
        // the connection task.
        let mut stream = select! {
            stream = connection.accept_uni() => match stream {
                Ok(stream) => stream,
                Err(e) => {
                    debug!("stream error: {e:?}");
                    break;
                }
            },
            _ = cancel.cancelled() => break,
        };

        qos.on_new_stream(&context).await;
        qos.on_stream_accepted(&context);
        stats.active_streams.fetch_add(1, Ordering::Relaxed);
        stats.total_new_streams.fetch_add(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/quic.rs (L647-677)
```rust
        loop {
            // Read the next chunks, waiting up to `wait_for_chunk_timeout`. If we don't get chunks
            // before then, we assume the stream is dead. This can only happen if there's severe
            // packet loss or the peer stops sending for whatever reason.
            let n_chunks = match tokio::select! {
                chunk = tokio::time::timeout(
                    wait_for_chunk_timeout,
                    stream.read_chunks(&mut chunks)) => chunk,

                // If the peer gets disconnected stop the task right away.
                _ = cancel.cancelled() => break,
            } {
                // read_chunk returned success
                Ok(Ok(chunk)) => chunk.unwrap_or(0),
                // read_chunk returned error
                Ok(Err(e)) => {
                    debug!("Received stream error: {e:?}");
                    stats
                        .total_stream_read_errors
                        .fetch_add(1, Ordering::Relaxed);
                    break;
                }
                // timeout elapsed
                Err(_) => {
                    debug!("Timeout in receiving on stream");
                    stats
                        .total_stream_read_timeouts
                        .fetch_add(1, Ordering::Relaxed);
                    break;
                }
            };
```

**File:** streamer/src/nonblocking/quic.rs (L1332-1370)
```rust
    #[tokio::test(flavor = "multi_thread")]
    async fn test_quic_stream_timeout() {
        agave_logger::setup();
        let SpawnTestServerResult {
            join_handle,
            receiver,
            server_address,
            stats,
            cancel,
        } = setup_quic_server(
            None,
            QuicStreamerConfig::default_for_tests(),
            SwQosConfig::default(),
        );

        let conn1 = make_client_endpoint(&server_address, None).await;
        assert_eq!(stats.active_streams.load(Ordering::Relaxed), 0);
        assert_eq!(stats.total_stream_read_timeouts.load(Ordering::Relaxed), 0);

        // Send one byte to start the stream
        let mut s1 = conn1.open_uni().await.unwrap();
        s1.write_all(&[0u8]).await.unwrap_or_default();

        // Wait long enough for the stream to timeout in receiving chunks
        let sleep_time = DEFAULT_WAIT_FOR_CHUNK_TIMEOUT * 2;
        sleep(sleep_time).await;

        // Test that the stream was created, but timed out in read
        assert_eq!(stats.active_streams.load(Ordering::Relaxed), 0);
        assert_ne!(stats.total_stream_read_timeouts.load(Ordering::Relaxed), 0);

        // Test that more writes to the stream will fail (i.e. the stream is no longer writable
        // after the timeouts)
        assert!(s1.write_all(&[0u8]).await.is_err());

        cancel.cancel();
        drop(receiver);
        join_handle.await.unwrap();
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L36-48)
```rust
// Empirically found max number of concurrent streams
// that seems to maximize TPS on GCE (higher values don't seem to
// give significant improvement or seem to impact stability)
pub const QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS: u32 = 128;
pub const QUIC_MIN_STAKED_CONCURRENT_STREAMS: u32 = 128;

// Set the maximum concurrent stream numbers to avoid excessive streams.
// The value was lowered from 2048 to reduce contention of the limited
// receive_window among the streams which is observed in CI bench-tests with
// forwarded packets from staked nodes.
pub const QUIC_MAX_STAKED_CONCURRENT_STREAMS: u32 = 512;

pub const QUIC_TOTAL_STAKED_CONCURRENT_STREAMS: u32 = 100_000;
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

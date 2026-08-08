### Title
Slow-loris partial-stream attack keeps the per-connection processing task permanently blocked, evading `wait_for_chunk_timeout` - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`handle_connection` processes uni-streams on a single connection strictly sequentially: it accepts one stream, then loops reading chunks from that stream until it finishes, errors, or times out, and only then goes back to `connection.accept_uni()` for the next stream. Because `wait_for_chunk_timeout` (`tokio::time::timeout(wait_for_chunk_timeout, stream.read_chunks(...))`) is reset on every successful chunk read regardless of size, an attacker can send a single byte just under the timeout interval forever, keeping the stream in the `Receiving` state indefinitely and never releasing that connection's processing loop to accept another stream.

### Finding Description
In `handle_connection` (streamer/src/nonblocking/quic.rs:610-711), the `'conn` loop accepts one stream via `connection.accept_uni()`, then enters an inner `loop` that calls:
```rust
tokio::time::timeout(wait_for_chunk_timeout, stream.read_chunks(&mut chunks))
``` [1](#0-0) 
This inner loop only exits when `handle_chunks` returns `StreamState::Finished`/`Err`, the stream errors, or the `timeout` elapses with **no data received at all** during the whole `wait_for_chunk_timeout` window. The timer is freshly re-armed on every loop iteration, so any successful `read_chunks` call — even one returning a single byte — resets the clock. An attacker can therefore open a uni-stream, write 1 byte, sleep for slightly less than `wait_for_chunk_timeout` (2s default, `DEFAULT_WAIT_FOR_CHUNK_TIMEOUT`, streamer/src/nonblocking/quic.rs:54), write another byte, and repeat forever, at a bandwidth cost of ~0.5 bytes/sec.

Critically, the outer `'conn` loop does not accept the next stream until the inner loop for the current stream terminates — the two loops are sequential, not concurrent, within a single connection task. This means the entire connection-handling task (one per connection) is occupied indefinitely by this single unfinished partial stream, and no other stream on that same connection can be serviced while the attack continues.

There is no complementary "maximum stream lifetime" or "maximum time since stream open" check independent of the chunk-arrival timer — only the chunk-to-chunk idle timeout is enforced, so a paced trickle of bytes defeats it by construction. `max_stream_data_bytes` only limits total bytes accepted, not stream duration, and does not stop this timing pattern.

### Impact Explanation
This is a resource-exhaustion / stream-slot starvation issue affecting the TPU QUIC ingress path (`streamer::nonblocking::quic`). An unstaked attacker can occupy the sequential per-connection stream-processing slot on each of the connections it is allowed to open (bounded by `max_connections_per_unstaked_peer`, e.g. `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8`, streamer/src/quic.rs:41) indefinitely at negligible bandwidth cost, preventing any further legitimate transaction on those specific connections from being processed. It does not by itself crash the validator or bypass sigverify/QoS, but it degrades stream throughput and starves legitimate senders sharing those connection slots, which fits an availability/DoS-adjacent bounty category rather than a consensus-safety one.

### Likelihood Explanation
Preconditions match the stated threat model: an unstaked remote client with plain QUIC/UDP access to the leader's public TPU port, using only `open_uni()` + `write` + `sleep`, no privileged access needed. The attack is fully repeatable and trivially automatable (fuzz slow-write timing against `wait_for_chunk_timeout`), and the existing test `test_quic_stream_timeout` (streamer/src/nonblocking/quic.rs:1332-1370) demonstrates the exact mechanism used by the defense (per-chunk timeout) that this attack evades by always sending before the deadline.

### Recommendation
Add a stream-lifetime (wall-clock) cap independent of chunk-arrival timing — e.g., track stream start time and enforce a maximum total duration for a stream to remain unfinished (bounded multiple of `wait_for_chunk_timeout`), closing/aborting the stream and advancing to the next `accept_uni()` regardless of trickle traffic. Alternatively/additionally, decouple stream acceptance from stream processing per connection (e.g., process multiple streams concurrently with a bounded per-connection concurrency budget) so a single slow-loris stream cannot block the whole connection's stream intake.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (test module)
#[tokio::test(flavor = "multi_thread")]
async fn test_quic_slow_loris_partial_stream_never_times_out() {
    agave_logger::setup();
    let SpawnTestServerResult {
        join_handle,
        receiver,
        server_address,
        stats,
        cancel,
    } = setup_quic_server(
        None,
        QuicStreamerConfig::default_for_tests(), // wait_for_chunk_timeout = DEFAULT_WAIT_FOR_CHUNK_TIMEOUT (2s)
        SwQosConfig::default(),
    );

    let conn1 = make_client_endpoint(&server_address, None).await;
    let mut s1 = conn1.open_uni().await.unwrap();

    // Attacker sends 1 byte just under the timeout, repeatedly, for well beyond
    // several multiples of wait_for_chunk_timeout.
    for _ in 0..10 {
        s1.write_all(&[0u8]).await.unwrap();
        sleep(DEFAULT_WAIT_FOR_CHUNK_TIMEOUT - Duration::from_millis(200)).await;
    }

    // Assertion of the bug: no timeout has fired, the stream slot is still held,
    // and — because handle_connection processes streams sequentially per
    // connection — a second stream opened on the SAME connection is never
    // even accepted/serviced within a bounded multiple of wait_for_chunk_timeout.
    assert_eq!(stats.total_stream_read_timeouts.load(Ordering::Relaxed), 0);
    assert_eq!(stats.active_streams.load(Ordering::Relaxed), 1);

    let mut s2 = conn1.open_uni().await.unwrap();
    s2.write_all(b"complete-tx-bytes").await.unwrap();
    s2.finish().unwrap();

    // Expected (fixed) behavior: s2 gets processed within bounded time.
    // Actual (buggy) behavior: s2 is never read because the connection task
    // is still stuck servicing s1's partial stream.
    let recv_result = tokio::time::timeout(Duration::from_secs(3), async {
        receiver.recv()
    }).await;
    assert!(recv_result.is_ok(), "legitimate stream on same connection starved by slow-loris partial stream");

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected outcome on the current (vulnerable) code: `total_stream_read_timeouts` stays at 0 indefinitely while the attacker paces sub-timeout writes, and the second, complete stream `s2` is starved because the connection's single processing loop never returns to `accept_uni()` while `s1` remains open. A fix should bound stream-open-to-completion wall-clock time regardless of attacker pacing.

### Citations

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

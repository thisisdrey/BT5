Confirmed: when the last connection entry for a given `ConnectionTableKey` (IP or Pubkey) is removed, the entry is dropped from the table entirely via `e.swap_remove_entry()` [1](#0-0) . When a fresh connection later arrives from the same key, `try_add_connection` looks up `connection_entry.first()`; since the vector is empty/absent, it falls through to `stream_counter_factory`, creating a brand-new `ConnectionStreamCounter` with `stream_count = 0` and `last_throttling_instant = Instant::now()` [2](#0-1) [3](#0-2) .

### Title
Per-IP stream throttle counter reset via connection churn allows unstaked attacker to bypass `max_streams_per_throttling_interval` - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
`throttle_stream` enforces the stream-rate cap using a `ConnectionStreamCounter` that is shared across connections keyed by IP/pubkey only while at least one connection entry for that key remains in the `ConnectionTable`. If the attacker closes/resets all its connections for that key, the entry is evicted from the table, and the very next connection from the same IP creates a brand-new counter starting at zero, discarding any accumulated `stream_count` and throttling window state.

### Finding Description
`SwQos::on_new_stream` calls `throttle_stream` with a `stream_counter: &Arc<ConnectionStreamCounter>` taken from `SwQosConnectionContext.stream_counter`, which was obtained during `try_add_connection`/`cache_new_connection` [4](#0-3) [5](#0-4) . That counter is fetched via `ConnectionTable::try_add_connection`, which reuses the counter from `connection_entry.first()` if one exists for the key, otherwise calls the `stream_counter_factory` closure to build a fresh one [6](#0-5) .

`ConnectionTable::remove_connection` (invoked from `SwQos::remove_connection` when a connection's task exits via `handle_connection`'s cleanup path [7](#0-6) , itself triggered when `handle_connection`'s `'conn` loop breaks on stream/connection error [8](#0-7) ) evicts the entire table entry for the key once its connection list becomes empty: `if e_ref.is_empty() { e.swap_remove_entry(); }` [9](#0-8) .

An unprivileged remote attacker opening exactly one connection per source IP (the default unstaked config already limits `max_connections_per_unstaked_peer`, but this does not prevent sequential churn) can therefore:
1. Open a connection, send streams until `stream_counter.stream_count` nears/exceeds `max_streams_per_throttling_interval`, triggering `throttle_stream`'s sleep path [10](#0-9) .
2. Instead of waiting out `sleep(throttle_duration).await`, immediately close/reset the QUIC connection (e.g., via `connection.close()`/idle timeout-independent abrupt drop), causing `handle_connection`'s loop to break and `remove_connection` to run, evicting the now-empty table entry for that IP.
3. Open a new connection from the same source IP. `try_add_connection` finds no existing entry for the key and manufactures a fresh `ConnectionStreamCounter::new()` with `stream_count = 0` and a new `last_throttling_instant` [3](#0-2) , so `throttle_stream`'s `streams_read_in_throttle_interval >= max_streams_per_throttling_interval` check starts over from zero rather than being enforced across the wall-clock `STREAM_THROTTLING_INTERVAL` window.
4. Repeat, keeping stream submission rate near the per-connection burst limit indefinitely without ever waiting through a full throttle sleep, evading the intended global-per-source stream rate cap tracked in `StakedStreamLoadEMA`/`available_load_capacity_in_throttling_duration` for unstaked peers.

Existing guards do not stop this: `max_connections_per_unstaked_peer` limits concurrent connections, not sequential churn; `prune_unstaked_connections_and_add_new_connection` only prunes when over capacity, not on churn; and there is no per-IP counter persistence independent of the connection table's liveness.

### Impact Explanation
This is a QoS/rate-limit evasion: an unstaked, unprivileged attacker can sustain a higher effective stream/packet ingestion rate into the sigverify pipeline than the configured `max_streams_per_throttling_interval` (derived from `MAX_UNSTAKED_TPS`/`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration`) is meant to permit, by resetting the per-IP counter through connection churn. This matches the "QoS evasion" bounty category — it does not itself cause a panic, deadlock, or invalid block, but it defeats a rate-limiting invariant intended to bound resource consumption by unstaked/low-stake senders on the TPU ingress path.

### Likelihood Explanation
Feasible for a single unprivileged remote attacker with no stake and no special network position — it only requires the ability to open/close QUIC uni-streams and reset connections at will, and to time the reset to occur exactly when `throttle_stream` would otherwise sleep. It is repeatable indefinitely (bounded only by how fast QUIC connection setup/teardown can be churned, and by `max_connections_per_unstaked_peer`/handshake cost), though the achievable gain is capped by connection-establishment overhead (TLS/QUIC handshake cost per churn cycle), which somewhat limits — but does not eliminate — the practical amplification.

### Recommendation
Persist per-IP/per-pubkey throttling state independently of `ConnectionTable` entry liveness (e.g., store `ConnectionStreamCounter` in a separate long-lived map keyed by IP/pubkey with its own eviction policy/TTL, or retain the empty table entry with its counter for at least one throttling interval before eviction) so that closing all connections for a key does not reset `stream_count`/`last_throttling_instant`.

### Proof of Concept
Rust integration test plan (extending the existing `streamer/src/nonblocking/quic.rs` test harness, e.g. building on `test_throttling_check_no_packet_drop` [11](#0-10) ):
1. Spin up a QUIC server via `setup_quic_server` with `SwQosConfig::default_for_tests()`.
2. From a single client IP, open a connection, send streams until `stats.throttled_unstaked_streams` increments once (confirming the counter hit `max_streams_per_throttling_interval`).
3. Immediately close the QUIC connection (`connection.close(...)`) rather than waiting for `sleep(throttle_duration)` to elapse, then open a brand-new `Connection` to the same server from the same client IP within the same `STREAM_THROTTLING_INTERVAL` window.
4. Send another burst of streams equal to `max_streams_per_throttling_interval` and assert they are accepted without triggering `throttled_unstaked_streams` again.
5. Assert that total packets received by `receiver` (packet_batch_sender) within one wall-clock `STREAM_THROTTLING_INTERVAL` window exceeds `max_streams_per_throttling_interval` — demonstrating the aggregate rate cap is bypassed by churn, whereas a control run without connection churn stays bounded.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L610-724)
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

        let mut meta = Meta::default();
        meta.set_socket_addr(&remote_address);
        meta.set_from_staked_node(matches!(peer_type, ConnectionPeerType::Staked(_)));
        if let Some(pubkey) = context.remote_pubkey() {
            meta.set_remote_pubkey(pubkey);
        }

        let mut accum = PacketAccumulator::new(meta);
        // Virtually all small transactions will fit in 1 chunk. Larger transactions will fit in 1
        // or 2 chunks if the first chunk starts towards the end of a datagram. A small number of
        // transaction will have other protocol frames inserted in the middle. Empirically it's been
        // observed that 4 is the maximum number of chunks txs get split into.
        //
        // Bytes values are small, so overall the array takes only 128 bytes, and the "cost" of
        // overallocating a few bytes is negligible compared to the cost of having to do multiple
        // read_chunks() calls.
        let mut chunks: [Bytes; 4] = array::from_fn(|_| Bytes::new());

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

            match handle_chunks(
                // Bytes::clone() is a cheap atomic inc
                chunks.iter().take(n_chunks).cloned(),
                &mut accum,
                rtt,
                &packet_sender,
                &stats,
                peer_type,
                max_stream_data_bytes,
            ) {
                // The stream is finished, break out of the loop and close the stream.
                Ok(StreamState::Finished) => {
                    qos.on_stream_finished(&context);
                    break;
                }
                // The stream is still active, continue reading.
                Ok(StreamState::Receiving) => {}
                Err(_) => {
                    // Disconnect peers that send invalid streams.
                    connection.close(
                        CONNECTION_CLOSE_CODE_INVALID_STREAM.into(),
                        CONNECTION_CLOSE_REASON_INVALID_STREAM,
                    );
                    stats.active_streams.fetch_sub(1, Ordering::Relaxed);
                    qos.on_stream_error(&context);
                    break 'conn;
                }
            }
        }

        stats.active_streams.fetch_sub(1, Ordering::Relaxed);
        qos.on_stream_closed(&context);
    }

    let removed_connection_count = qos.remove_connection(&context, connection).await;
    if removed_connection_count > 0 {
        stats
            .connection_removed
            .fetch_add(removed_connection_count, Ordering::Relaxed);
    } else {
        stats
            .connection_remove_failed
            .fetch_add(1, Ordering::Relaxed);
    }
    stats.total_connections.fetch_sub(1, Ordering::Relaxed);
}
```

**File:** streamer/src/nonblocking/quic.rs (L1008-1030)
```rust
    pub(crate) fn try_add_connection<F: FnOnce() -> Arc<S>>(
        &mut self,
        key: ConnectionTableKey,
        port: u16,
        client_connection_tracker: ClientConnectionTracker,
        connection: Option<Connection>,
        peer_type: ConnectionPeerType,
        last_update: Arc<AtomicU64>,
        max_connections_per_peer: usize,
        stream_counter_factory: F,
    ) -> Option<(Arc<AtomicU64>, CancellationToken, Arc<S>)> {
        let connection_entry = self.table.entry(key).or_default();
        let has_connection_capacity = connection_entry
            .len()
            .checked_add(1)
            .map(|c| c <= max_connections_per_peer)
            .unwrap_or(false);
        if has_connection_capacity {
            let cancel = self.cancel.child_token();
            let stream_counter = connection_entry
                .first()
                .map(|entry| entry.stream_counter.clone())
                .unwrap_or_else(stream_counter_factory);
```

**File:** streamer/src/nonblocking/quic.rs (L1077-1080)
```rust
            let new_size = e_ref.len();
            if e_ref.is_empty() {
                e.swap_remove_entry();
            }
```

**File:** streamer/src/nonblocking/quic.rs (L2009-2059)
```rust
    #[tokio::test(flavor = "multi_thread")]
    async fn test_throttling_check_no_packet_drop() {
        agave_logger::setup_with_default_filter();

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

        let client_connection = make_client_endpoint(&server_address, None).await;

        // unstaked connection can handle up to 100tps, so we should send in ~1s.
        let expected_num_txs = 100;
        let start_time = tokio::time::Instant::now();
        for i in 0..expected_num_txs {
            let mut send_stream = client_connection.open_uni().await.unwrap();
            let data = format!("{i}").into_bytes();
            send_stream.write_all(&data).await.unwrap();
            send_stream.finish().unwrap();
        }
        let elapsed_sending: f64 = start_time.elapsed().as_secs_f64();
        info!("Elapsed sending: {elapsed_sending}");

        // check that delivered all of them
        let start_time = tokio::time::Instant::now();
        let mut num_txs_received = 0;
        while num_txs_received < expected_num_txs && start_time.elapsed() < Duration::from_secs(2) {
            if let Ok(packets) = receiver.try_recv() {
                num_txs_received += packets.len();
            } else {
                sleep(Duration::from_millis(100)).await;
            }
        }
        assert_eq!(expected_num_txs, num_txs_received);

        cancel.cancel();
        join_handle.await.unwrap();

        assert_eq!(
            stats.total_new_streams.load(Ordering::Relaxed),
            expected_num_txs
        );
        assert!(stats.throttled_unstaked_streams.load(Ordering::Relaxed) > 0);
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L203-209)
```rust
impl ConnectionStreamCounter {
    pub fn new() -> Self {
        Self {
            stream_count: AtomicU64::default(),
            last_throttling_instant: RwLock::new(tokio::time::Instant::now()),
        }
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L240-269)
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
```

**File:** streamer/src/nonblocking/swqos.rs (L209-219)
```rust
        if let Some((last_update, cancel_connection, stream_counter)) = connection_table_l
            .try_add_connection(
                ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey),
                remote_addr.port(),
                client_connection_tracker,
                Some(connection.clone()),
                conn_context.peer_type(),
                conn_context.last_update.clone(),
                max_connections_per_peer,
                || Arc::new(ConnectionStreamCounter::new()),
            )
```

**File:** streamer/src/nonblocking/swqos.rs (L464-488)
```rust
    #[allow(clippy::manual_async_fn)]
    fn remove_connection(
        &self,
        conn_context: &SwQosConnectionContext,
        connection: Connection,
    ) -> impl Future<Output = usize> + Send {
        async move {
            let mut lock = if conn_context.in_staked_table {
                self.staked_connection_table.lock().await
            } else {
                self.unstaked_connection_table.lock().await
            };

            let stable_id = connection.stable_id();
            let remote_addr = conn_context.remote_address;

            let removed_count = lock.remove_connection(
                ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey()),
                remote_addr.port(),
                stable_id,
            );
            update_open_connections_stat(&self.stats, &lock);
            removed_count
        }
    }
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

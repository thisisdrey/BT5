### Title
Unstaked QUIC clients can bypass the per-connection stream-throttling window by closing and reopening connections faster than `STREAM_THROTTLING_INTERVAL` - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
`ConnectionStreamCounter::stream_count` is only preserved for the lifetime that at least one connection from a given `ConnectionTableKey` remains in `ConnectionTable`. When the last connection for a key is removed, the map entry is dropped and the shared `Arc<ConnectionStreamCounter>` is deallocated. A subsequent connection from the same unstaked IP triggers `stream_counter_factory()` in `try_add_connection`, allocating a brand-new counter with `stream_count = 0`, effectively resetting the throttling window regardless of how much time has elapsed.

### Finding Description
`SwQos::try_add_connection` → `ConnectionTable::try_add_connection` reuses the existing `stream_counter` only when an entry already exists for the connection's `ConnectionTableKey` (IP for unstaked clients): [1](#0-0) 

If the entry vector is empty (i.e., no connections currently open from that IP), `connection_entry.first()` returns `None` and the fallback `stream_counter_factory` (`|| Arc::new(ConnectionStreamCounter::new())`) is invoked, producing a fresh counter with `stream_count = 0`: [2](#0-1) [3](#0-2) 

`ConnectionTable::remove_connection`, called when the client closes its connection (via `SwQos::remove_connection`), removes the table entry entirely once its connection vector becomes empty: [4](#0-3) [5](#0-4) 

Because the `Arc<ConnectionStreamCounter>` is only held by `ConnectionEntry` instances in the table (and transiently by in-flight stream handlers), once the table entry is removed and any in-flight stream tasks finish, the counter is dropped. The throttling logic in `throttle_stream`/`reset_throttling_params_if_needed` has no persistence outside this per-entry `Arc`, so there is no independent, connection-churn-resistant record of how many streams a given IP has consumed: [6](#0-5) 

Exploit flow for an unstaked attacker IP:
1. Open a QUIC connection; `try_add_connection` allocates a fresh `ConnectionStreamCounter` (first connection from this IP).
2. Open streams up to `max_streams_per_throttling_interval` (the unstaked cap) within the current window.
3. Immediately close the connection (client-initiated), which triggers `remove_connection`, dropping the empty table entry and the shared counter.
4. Open a new connection from the same IP before `STREAM_THROTTLING_INTERVAL` elapses; a brand-new `ConnectionStreamCounter::new()` is allocated with `stream_count = 0`.
5. Repeat, achieving a continuous stream rate that is limited only by QUIC connection-establishment overhead and `max_connections_per_unstaked_peer`, not by the intended per-window unstaked cap.

The `max_connections_per_unstaked_peer` and global `max_unstaked_connections` pruning limits govern how many *concurrent* connections an IP can hold, but do not prevent rapid sequential connection churn from resetting the stream counter, since a fully-closed connection frees the table slot entirely.

### Impact Explanation
This allows an unstaked, unauthenticated remote client to exceed the intended `MAX_UNSTAKED_TPS`-derived stream ingestion cap enforced by `throttle_stream`/`SwQos::on_new_stream`, by repeatedly opening and closing QUIC connections. Elevated unstaked stream ingestion increases work pushed into the QUIC read/stream-processing pipeline that feeds sigverify/banking stages, and undermines the QoS invariant that unstaked traffic from a single source is capped independent of connection churn. This matches the "unbounded/underpriced ingestion at the QUIC ingress layer allowing starvation of sigverify/banking threads" bounty category, though the magnitude of the bypass is bounded by QUIC handshake cost per connection cycle (this is not literally unbounded memory growth or a panic/deadlock, but a QoS-evasion of the rate limiter).

### Likelihood Explanation
Preconditions are minimal: an unstaked remote client with network access to the leader's public TPU/QUIC port, no stake, no keys, no special config. The attacker only needs to repeatedly perform connect → send bursts up to the throttling cap → close → reconnect, cycling faster than `STREAM_THROTTLING_INTERVAL`. This is fully client-controlled and repeatable; the main limiting factor is QUIC connection-establishment latency/cost, which is nonzero but well within reach of a single low-latency attacker, especially since `max_connections_per_unstaked_peer` does not prevent sequential (non-concurrent) reconnections.

### Recommendation
Decouple the per-IP/per-key stream throttling state from connection-table membership. Maintain the `ConnectionStreamCounter` (or an equivalent rate-limit state) in a separate, longer-lived map keyed by IP (or by `ConnectionTableKey`) that persists across connection open/close events for some minimum retention period (e.g., at least `STREAM_THROTTLING_INTERVAL`), instead of allocating a fresh counter whenever the connection-table entry for that key becomes empty. Alternatively, only allow the counter to reset based on elapsed time (`reset_throttling_params_if_needed`'s existing logic) and never re-create it from scratch on reconnection within the same throttling epoch.

### Proof of Concept
Integration test plan (extending existing tests like `test_throttling_check_no_packet_drop` in `streamer/src/nonblocking/quic.rs`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_unstaked_stream_throttle_bypass_via_connection_churn() {
    // Spin up a QUIC server with SwQos default config (unstaked cap = N streams / STREAM_THROTTLING_INTERVAL).
    // Loop:
    //   1. Open a new client QUIC connection to the server (same source IP).
    //   2. Open and finish `N` uni streams (the unstaked per-interval cap) rapidly.
    //   3. Close the connection immediately (client_connection.close(...)).
    //   4. Repeat step 1-3 in a tight loop for a duration >> STREAM_THROTTLING_INTERVAL,
    //      reconnecting faster than STREAM_THROTTLING_INTERVAL between cycles.
    // Assert: total accepted streams received by the server over the test duration
    // significantly exceeds `N * (test_duration / STREAM_THROTTLING_INTERVAL)`,
    // i.e., the aggregate accepted unstaked stream rate exceeds the configured
    // max_unstaked_load_in_throttling_window-derived cap, demonstrating the counter
    // reset bypasses the intended throttle.
}
```
Expected result confirming the bug: total streams accepted > cap × number of intervals elapsed, because each reconnect from the same IP allocates a fresh `ConnectionStreamCounter` with `stream_count = 0` via `ConnectionTable::try_add_connection`'s `stream_counter_factory` fallback.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L1019-1030)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L1060-1080)
```rust
        if let Entry::Occupied(mut e) = self.table.entry(key) {
            let e_ref = e.get_mut();
            let old_size = e_ref.len();

            e_ref.retain(|connection_entry| {
                // Retain the connection entry if the port is different, or if the connection's
                // stable_id doesn't match the provided stable_id.
                // (Some unit tests do not fill in a valid connection in the table. To support that,
                // if the connection is none, the stable_id check is ignored. i.e. if the port matches,
                // the connection gets removed)
                connection_entry.port != port
                    || connection_entry
                        .connection
                        .as_ref()
                        .and_then(|connection| (connection.stable_id() != stable_id).then_some(0))
                        .is_some()
            });
            let new_size = e_ref.len();
            if e_ref.is_empty() {
                e.swap_remove_entry();
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L211-271)
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
}

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

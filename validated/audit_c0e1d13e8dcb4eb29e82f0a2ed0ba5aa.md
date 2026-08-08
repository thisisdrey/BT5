### Title
Per-source QUIC stream-rate throttle can be reset via connection churn, bypassing intended per-IP stream QoS - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
The per-connection stream throttle state (`ConnectionStreamCounter`) is stored in the `ConnectionTable` entry keyed by IP/pubkey and is only shared across *concurrently open* connections from the same source; once all connections for a key are closed, the table entry is removed and a brand-new `ConnectionStreamCounter` is created on the next connection. An unstaked attacker who fully closes and re-opens QUIC connections faster than the `STREAM_THROTTLING_INTERVAL` (100ms) natural reset can obtain repeated fresh throttling windows, effectively bypassing the intended per-source stream ingestion cap.

### Finding Description
`ConnectionTable::try_add_connection` reuses the existing `stream_counter` only when the `Vec<ConnectionEntry>` for the key is non-empty; otherwise it invokes `stream_counter_factory` to allocate a fresh `Arc<ConnectionStreamCounter>`: [1](#0-0) 

`ConnectionTable::remove_connection` removes the map entry entirely once the connection list for that key becomes empty: [2](#0-1) 

`throttle_stream` and `ConnectionStreamCounter::reset_throttling_params_if_needed` rely entirely on this per-entry counter to enforce `max_streams_per_throttling_interval` within `STREAM_THROTTLING_INTERVAL`: [3](#0-2) 

`SwQos::cache_new_connection` passes `|| Arc::new(ConnectionStreamCounter::new())` as the factory used by `try_add_connection`, confirming the fresh-counter-on-empty-entry behavior for the production QoS path: [4](#0-3) 

Exploit flow: an unstaked attacker opens a connection, bursts streams until `throttle_stream` starts sleeping (window exhausted), then fully closes the connection (triggering `remove_connection` → entry removal from the `ConnectionTable`), and immediately opens a new connection from the same IP. Because the table entry was removed, the new connection gets a fresh `ConnectionStreamCounter` with `stream_count = 0` and a reset `last_throttling_instant`, regaining full quota before the natural 100ms window would have expired.

This is only partially mitigated by the separate `ConnectionRateLimiter` (`max_connections_per_ipaddr_per_min`, default 8/min with a 10x burst allowance) and the global `overall_connection_rate_limiter`: [5](#0-4) [6](#0-5) 

These limiters bound connection establishment (default 8/min, burst 80), but do not prevent an attacker from using that permitted burst to churn several fresh `ConnectionStreamCounter`s within a very short interval, each granting a new full stream quota, before falling back to the steady per-minute connection rate.

### Impact Explanation
This allows a single unstaked source IP to exceed the intended `max_streams_per_throttling_interval` cap during connection-establishment bursts, degrading per-source fairness enforced by the QoS design (`SwQos`/`StakedStreamLoadEMA`). This matches the "QoS evasion" bounty category: an unprivileged, unstaked peer can consume disproportionately more stream/packet-processing capacity than the throttling design intends, though the effect is bounded by the existing per-IP/overall connection-rate limiters (default 8 connections/min, 80 burst), which caps the degree of amplification achievable.

### Likelihood Explanation
Feasible with only an unstaked identity and `max_connections_per_unstaked_peer` ≥ 1 (not even >1 is strictly required, since the entry is removed once empty). It is fully reproducible by any remote client able to open/close QUIC connections to the TPU port; no special privilege, stake, or race condition is required, though the magnitude of the bypass is capped by the connection-rate limiter's burst budget.

### Recommendation
Decouple the stream-throttle bookkeeping from connection-table entry lifetime: key `ConnectionStreamCounter` state by source IP (or client identity) in a separate long-lived map (e.g., similar to `ConnectionRateLimiter`'s `KeyedRateLimiter`) that persists across individual connection open/close events, rather than being dropped once the last connection for a key is removed from `ConnectionTable`.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test harness, using `setup_quic_server`/`make_client_endpoint`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_stream_throttle_bypass_via_connection_churn() {
    // Configure SwQosConfig with max_connections_per_unstaked_peer = 1
    // and a small max_streams_per_ms so throttling triggers quickly.
    let SpawnTestServerResult { server_address, stats, cancel, join_handle, receiver } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(), SwQosConfig::default());

    // Baseline: single long-lived connection sending streams as fast as possible
    // for N seconds -> measure total streams accepted (throttled_unstaked_streams > 0,
    // total accepted streams bounded by max_streams_per_throttling_interval * (N/0.1s)).

    // Attack: repeatedly open a fresh connection, burst
    // max_streams_per_throttling_interval streams immediately, then close the
    // connection and open a new one from the same IP (bounded by
    // ConnectionRateLimiter burst capacity), for the same wall-clock duration N.

    // Assertion: total streams accepted via churn > total streams accepted on the
    // single persistent connection for the same wall-clock window, demonstrating
    // that closing/reopening connections resets ConnectionStreamCounter and yields
    // extra quota beyond the intended per-source STREAM_THROTTLING_INTERVAL cap.
}
```
Expected result: the churn scenario accepts more streams per unit time from one IP than the single-connection baseline, violating the invariant that stream-rate limiting is enforced per source regardless of connection churn.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L270-281)
```rust
    let rate_limiter = Arc::new(ConnectionRateLimiter::new(
        quic_server_params.max_connections_per_ipaddr_per_min,
        // allow for 10x burst to make sure we can accommodate legitimate
        // bursts from container environments running multiple pods on same IP
        quic_server_params.max_connections_per_ipaddr_per_min * 10,
        num_shards,
    ));
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L1019-1041)
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
            connection_entry.push(ConnectionEntry::new(
                cancel.clone(),
                peer_type,
                last_update.clone(),
                port,
                client_connection_tracker,
                connection,
                stream_counter.clone(),
            ));
            self.total_size += 1;
            Some((last_update, cancel, stream_counter))
```

**File:** streamer/src/nonblocking/quic.rs (L1060-1087)
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
            let connections_removed = old_size.saturating_sub(new_size);
            self.total_size = self.total_size.saturating_sub(connections_removed);
            connections_removed
        } else {
            0
        }
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L203-271)
```rust
impl ConnectionStreamCounter {
    pub fn new() -> Self {
        Self {
            stream_count: AtomicU64::default(),
            last_throttling_instant: RwLock::new(tokio::time::Instant::now()),
        }
    }

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

**File:** streamer/src/quic.rs (L53-56)
```rust
/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```

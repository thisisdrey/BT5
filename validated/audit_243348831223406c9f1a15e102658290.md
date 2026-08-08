This is now confirmed. Key finding: `available_load_capacity_in_throttling_duration` for `ConnectionPeerType::Unstaked` always returns the constant `max_unstaked_load_in_throttling_window` [1](#0-0)  — this is a **fixed per-connection quota** derived from `MAX_UNSTAKED_TPS` (200 TPS) and `STREAM_THROTTLING_INTERVAL_MS` [2](#0-1) [3](#0-2) , not a globally-tracked shared budget across all unstaked connections. Unlike staked load (`increment_load` only adds to `load_in_recent_interval` when `peer_type.is_staked()`) [4](#0-3) , unstaked traffic is *not* metered into the global EMA at all — the cap is purely enforced by the **per-`ConnectionStreamCounter` `stream_count`** via `throttle_stream` [5](#0-4) .

And critically, `ConnectionTable::try_add_connection` only reuses an existing `stream_counter` if there is already a live entry for that `ConnectionTableKey`; if the entry has zero connections (i.e., all prior connections for that IP were removed), `remove_connection` deletes the whole map entry [6](#0-5) , and the next `try_add_connection` call falls to `stream_counter_factory()`, creating a **brand-new `ConnectionStreamCounter` with `stream_count = 0`** [7](#0-6) [8](#0-7) .

So the reasoning in the question is technically accurate for the code path, but the only gate against the churn is `ConnectionRateLimiter`, which defaults to `DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE = 8` per minute with a 10x burst allowance [9](#0-8) [10](#0-9) , plus a global `overall_connection_rate_limiter` (`MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND`) shared across *all* IPs [11](#0-10) .

### Title
Per-connection ConnectionStreamCounter reset via QUIC connection churn bypasses unstaked stream-rate throttling - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
The unstaked stream-rate cap enforced by `throttle_stream`/`ConnectionStreamCounter` is scoped per-connection-table-entry, not globally tracked in `StakedStreamLoadEMA`. Because `remove_connection` deletes the `ConnectionTableKey` entry once its connection list empties, a new connection for the same source IP receives a fresh `ConnectionStreamCounter` with `stream_count = 0`, allowing an attacker who reconnects to open a fresh burst of `max_unstaked_load_in_throttling_window` streams every reconnect cycle.

### Finding Description
`SwQos::try_add_connection` and `ConnectionTable::try_add_connection` cache the `ConnectionStreamCounter` per `ConnectionTableKey` (IP for unstaked peers), cloning it for co-existing connections from the same key but creating a fresh one via `stream_counter_factory` when no entry exists [12](#0-11) . `ConnectionTable::remove_connection` removes the map entry entirely once the last connection for that key is gone [13](#0-12) . `throttle_stream` only consults this per-entry `stream_count`, comparing it against `max_streams_per_throttling_interval` obtained from `StakedStreamLoadEMA::available_load_capacity_in_throttling_duration`, which for `ConnectionPeerType::Unstaked` is the constant `max_unstaked_load_in_throttling_window` — unrelated to any live global counter of unstaked stream volume [1](#0-0) [5](#0-4) . `increment_load` (called from `on_stream_accepted`) only feeds the EMA for staked peers (`if peer_type.is_staked()`) [4](#0-3) , confirming unstaked streams are never tallied into any shared/global counter — the only defense against unstaked burst repetition is the fixed per-connection-key quota, which resets on reconnect.

### Impact Explanation
This maps to a QoS-evasion / rate-limit-bypass category: an unstaked attacker can sustain an aggregate stream-admission rate on the leader's TPU QUIC port that exceeds the intended `MAX_UNSTAKED_TPS` (200) design budget by repeatedly reconnecting, degrading fair-share ingress bandwidth for legitimate unstaked senders and increasing load on downstream sigverify/banking stages. It does not by itself cause a panic, deadlock, unbounded memory growth, or verification bypass — it is a rate-limit/QoS evasion.

### Likelihood Explanation
Feasibility is gated by `ConnectionRateLimiter` (default 8 connections/IP/min, burst 80) [9](#0-8)  and the global `overall_connection_rate_limiter` (`MAX_CONNECTION_BURST`/`TOTAL_CONNECTIONS_PER_SECOND`, shared by all peers) [11](#0-10) . `STREAM_THROTTLING_INTERVAL_MS` is 100ms [14](#0-13) , meaning to exploit the reset every window an attacker would need ~10 reconnects/sec from a single IP, far exceeding the default 8/min per-IP budget (burst covers only the first ~80, after which the attacker is throttled back to ~1 connection every 7.5s — no longer materially increasing throughput beyond the steady-state per-connection cap). So sustained, indefinite exploitation at meaningful multiples of `MAX_UNSTAKED_TPS` is not achievable under default config; only a short burst (bounded by the 10x burst allowance) is possible before `ConnectionRateLimiter`/`overall_connection_rate_limiter` throttle reconnections back down near the intended budget.

### Recommendation
Track unstaked stream admission in a rate-limiter keyed by IP (or in a shared/global unstaked counter within `StakedStreamLoadEMA`, mirroring staked `load_in_recent_interval`) that survives connection teardown, rather than deriving the cap solely from a per-`ConnectionTableKey` entry's `ConnectionStreamCounter` that is discarded when connections close. Alternatively, persist a short-lived "last stream_count / last_throttling_instant" record per source IP across reconnects (e.g., a small TTL-based `KeyedRateLimiter<IpAddr>` similar to `ConnectionRateLimiter`) so a fresh QUIC connection does not automatically zero the throttle window.

### Proof of Concept
Integration test plan (extending `streamer/src/nonblocking/quic.rs` test module or `swqos.rs` tests):
```rust
#[tokio::test]
async fn test_stream_throttle_reset_via_reconnect() {
    // 1. Spawn SwQos-backed QUIC server with:
    //    - max_connections_per_ipaddr_per_min set high enough to allow N reconnects
    //      within test duration (simulate burst allowance),
    //    - default unstaked stream throttling config (MAX_UNSTAKED_TPS-derived).
    // 2. For several iterations within one STREAM_THROTTLING_INTERVAL_MS-sized budget:
    //    a. Open a new QUIC connection from the same source IP.
    //    b. Open uni streams up to `max_unstaked_load_in_throttling_window` on this connection.
    //    c. Assert none of them were throttled (stats.throttled_unstaked_streams unchanged).
    //    d. Close the connection immediately (drop) before the next stream would sleep.
    //    e. Reopen a new connection (repeat).
    // 3. Assert that stats.throttled_unstaked_streams stays at 0 across all iterations
    //    while total streams admitted / elapsed_time exceeds MAX_UNSTAKED_TPS,
    //    demonstrating that reconnection resets ConnectionStreamCounter and lets the
    //    per-source rate exceed the intended cap — bounded only by ConnectionRateLimiter's
    //    burst allowance, not by StakedStreamLoadEMA/throttle_stream.
}
```
Expected result under current code: while within `ConnectionRateLimiter`'s burst window, the aggregate admitted-stream rate briefly exceeds `MAX_UNSTAKED_TPS`; once the burst allowance is exhausted, `rate_limiter.register_connection` starts rejecting new connections, capping further reconnection-driven evasion to the steady per-IP-per-minute rate.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-23)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;

pub const STREAM_THROTTLING_INTERVAL_MS: u64 = 100;
pub const STREAM_THROTTLING_INTERVAL: Duration =
    Duration::from_millis(STREAM_THROTTLING_INTERVAL_MS);
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L64-68)
```rust
        let max_unstaked_load_in_throttling_window = if allow_unstaked_streams {
            MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
        } else {
            0
        };
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L160-165)
```rust
    pub(crate) fn increment_load(&self, peer_type: ConnectionPeerType) {
        if peer_type.is_staked() {
            self.load_in_recent_interval.fetch_add(1, Ordering::Relaxed);
        }
        self.update_ema_if_needed();
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L167-174)
```rust
    pub(crate) fn available_load_capacity_in_throttling_duration(
        &self,
        peer_type: ConnectionPeerType,
        total_stake: u64,
    ) -> u64 {
        match peer_type {
            ConnectionPeerType::Unstaked => self.max_unstaked_load_in_throttling_window,
            ConnectionPeerType::Staked(stake) => {
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

**File:** streamer/src/nonblocking/quic.rs (L270-276)
```rust
    let rate_limiter = Arc::new(ConnectionRateLimiter::new(
        quic_server_params.max_connections_per_ipaddr_per_min,
        // allow for 10x burst to make sure we can accommodate legitimate
        // bursts from container environments running multiple pods on same IP
        quic_server_params.max_connections_per_ipaddr_per_min * 10,
        num_shards,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L1008-1041)
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

**File:** streamer/src/nonblocking/quic.rs (L1054-1087)
```rust
    pub(crate) fn remove_connection(
        &mut self,
        key: ConnectionTableKey,
        port: u16,
        stable_id: usize,
    ) -> usize {
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

**File:** streamer/src/quic.rs (L53-56)
```rust
/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```

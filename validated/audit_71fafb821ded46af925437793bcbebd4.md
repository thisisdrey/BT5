### Title
Staked-peer QUIC stream throttle can be reset via repeated connect/disconnect cycling, enabling QoS evasion — ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
The external report describes gaming a per-account reward budget by rapidly staking and withdrawing to repeatedly "reset" the accounting state and extract more than the intended share. The equivalent unprivileged-user primitive in agave is the QUIC stream-throttling budget assigned to each staked connection: it is tracked in a per-connection `ConnectionStreamCounter` that starts fresh (`stream_count = 0`) whenever a brand-new connection table entry is created for a given `(ip, pubkey)` key. A peer that closes all of its connections and reconnects gets an entirely new counter, letting it exceed the intended per-`STREAM_THROTTLING_INTERVAL` (100ms) stream budget by repeating connect/disconnect cycles within the per-IP connection-rate-limiter's burst allowance.

### Finding Description
Each staked (ip, pubkey) key in the `ConnectionTable` shares a single `ConnectionStreamCounter` across all *concurrently open* connections for that key: `try_add_connection` reuses the `stream_counter` of the first existing entry, but calls `stream_counter_factory` (i.e., allocates a fresh `Arc::new(ConnectionStreamCounter::new())`) whenever there is no existing entry for that key [1](#0-0) .

`ConnectionStreamCounter::new()` always starts with `stream_count = 0` and a fresh `last_throttling_instant` [2](#0-1) . `throttle_stream` only sleeps a peer once its counter has accumulated `max_streams_per_throttling_interval` (derived from stake share) since the counter's own `last_throttling_instant`, and `reset_throttling_params_if_needed` only zeroes the counter once more than 100ms has elapsed since that instant [3](#0-2) [4](#0-3) .

Because the counter (and its clock) is per-connection-table-entry rather than per-pubkey-over-time, a client can:
1. Open a connection, burst up to its full per-interval stream quota (`available_load_capacity_in_throttling_duration`, proportional to stake) [5](#0-4) .
2. Close the connection (removing the table entry).
3. Reconnect before the 100ms window elapses — the new connection gets a brand-new `ConnectionStreamCounter` with `stream_count = 0`, so it can immediately burst its full quota again.

The only defenses against rapid reconnection are the per-IP `ConnectionRateLimiter` (default `max_connections_per_ipaddr_per_min`, with a 10x burst allowance) and the global `overall_connection_rate_limiter` token bucket (`TOTAL_CONNECTIONS_PER_SECOND = 2500`, `MAX_CONNECTION_BURST = 1000`) [6](#0-5) [7](#0-6) . Neither of these is scoped to the per-stream throttling window (100ms); they only bound reconnects per-minute/per-second, so within their burst allowances a peer can still perform several connect→burst→disconnect cycles inside a single 100ms throttling window (or several consecutive windows), each time resetting its stream quota and exceeding the intended stake-proportional TPS allocation that `StakedStreamLoadEMA` is meant to enforce for fairness among staked peers [8](#0-7) .

### Impact Explanation
This is a QoS evasion within the staked-connection stream-throttling mechanism: a single low/medium-stake peer can transiently claim disproportionately more of the shared staked-stream budget than its stake share entitles it to, by cycling connections faster than the 100ms throttling window, degrading fairness for other staked senders during that peer's leader/window traffic. It does not cause a crash, unbounded memory, or invalid ledger state — impact is bounded to unfair bandwidth allocation, gated by connection-rate-limiter burst budgets.

### Likelihood Explanation
Reachable by any unprivileged staked client with a valid identity keypair and stake — no special/operator privileges required; it only needs the ability to open/close QUIC connections to the TPU port, which is standard client behavior. Exploitability is bounded by the connection-rate-limiter burst sizes (`max_connections_per_ipaddr_per_min * 10` and `MAX_CONNECTION_BURST = 1000`), so gains are proportional to how many reconnect bursts a peer can perform, not unbounded.

### Recommendation
Track the stream-throttling counter and its window start time per (ip, pubkey) identity independently of the connection-table entry lifetime (e.g., keep the counter alive for some cooldown period after the last connection for that key is removed, or persist it in a separate map keyed by pubkey/ip that outlives individual connections), so that closing and reopening a connection cannot reset the throttling window before it naturally expires.

### Proof of Concept
1. As a staked client, open a QUIC connection to a validator's TPU port and send streams up to the computed `available_load_capacity_in_throttling_duration` quota for the current 100ms window.
2. Immediately close the connection.
3. Reconnect (within the per-IP rate limiter's burst budget) before 100ms has elapsed; observe (via `stats.stream_load_ema`/`throttled_staked_streams` counters, or packet-batch delivery counts) that the new connection is granted a fresh, zeroed `ConnectionStreamCounter` and is not throttled even though the aggregate streams sent by this pubkey within the 100ms window exceed its stake-proportional allocation.
4. Repeat within the connection-rate-limiter burst budget to demonstrate sustained over-quota throughput relative to a stable peer with equal stake that keeps a single connection open.

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

**File:** streamer/src/nonblocking/quic.rs (L346-369)
```rust
            // check overall connection request rate limiter
            if overall_connection_rate_limiter.current_tokens() == 0 {
                stats
                    .connection_rate_limited_across_all
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to overall rate limit.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
            // then perform per IpAddr rate limiting
            if !rate_limiter.is_allowed(&incoming.remote_address().ip()) {
                stats
                    .connection_rate_limited_per_ipaddr
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to per-IP rate limiting.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L167-188)
```rust
    pub(crate) fn available_load_capacity_in_throttling_duration(
        &self,
        peer_type: ConnectionPeerType,
        total_stake: u64,
    ) -> u64 {
        match peer_type {
            ConnectionPeerType::Unstaked => self.max_unstaked_load_in_throttling_window,
            ConnectionPeerType::Staked(stake) => {
                if self.staked_throttling_enabled.load(Ordering::Relaxed) {
                    // 1 is added to `max_unstaked_load_in_throttling_window` to guarantee that staked
                    // clients get at least 1 more number of streams than unstaked connections.
                    self.max_staked_load_in_throttling_window
                        .saturating_mul(stake)
                        .checked_div(total_stake)
                        .unwrap_or(self.max_unstaked_load_in_throttling_window + 1)
                        .max(self.max_unstaked_load_in_throttling_window + 1)
                } else {
                    self.max_staked_load_in_throttling_window
                }
            }
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

**File:** streamer/src/nonblocking/swqos.rs (L181-222)
```rust
impl SwQos {
    fn cache_new_connection(
        &self,
        client_connection_tracker: ClientConnectionTracker,
        connection: &Connection,
        mut connection_table_l: MutexGuard<ConnectionTable<ConnectionStreamCounter>>,
        conn_context: &SwQosConnectionContext,
    ) -> Result<
        (
            Arc<AtomicU64>,
            CancellationToken,
            Arc<ConnectionStreamCounter>,
        ),
        ConnectionHandlerError,
    > {
        // get current RTT and limit it to MAX_RTT_MS right away
        let rtt_millis = connection.rtt().as_millis().min(MAX_RTT_MS as u128) as u32;
        let max_uni_streams = VarInt::from_u32(compute_max_allowed_uni_streams_with_rtt(
            rtt_millis,
            conn_context.peer_type(),
            conn_context.total_stake,
        ));
        let remote_addr = conn_context.remote_address;

        let max_connections_per_peer = match conn_context.peer_type() {
            ConnectionPeerType::Unstaked => self.config.max_connections_per_unstaked_peer,
            ConnectionPeerType::Staked(_) => self.config.max_connections_per_staked_peer,
        };
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
        {
            update_open_connections_stat(&self.stats, &connection_table_l);
            drop(connection_table_l);
```

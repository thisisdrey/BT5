### Title
Per-IP (not global) unstaked stream throttling allows aggregate TPU flood via IP/connection fan-out - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
The unstaked stream-rate budget (`MAX_UNSTAKED_TPS`, enforced through `StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` and `throttle_stream`) is applied independently per `ConnectionTableKey` (effectively per source IP for unstaked/pubkey-less connections), not against any shared/global unstaked counter. An attacker who opens many distinct unstaked source IPs, each staying under `max_connections_per_unstaked_peer` and `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`, gets a full, independent per-IP stream budget for every IP, so the aggregate accepted unstaked stream rate scales linearly with the number of distinct IPs rather than being capped at the intended `EXPECTED_UNSTAKED_STREAMS_RATIO` share of `max_streams_per_ms`.

### Finding Description
`StakedStreamLoadEMA::new` computes `max_unstaked_load_in_throttling_window = MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` as a fixed constant [1](#0-0) . This same fixed value is returned unconditionally for every `ConnectionPeerType::Unstaked` stream via `available_load_capacity_in_throttling_duration` [2](#0-1) .

That per-call budget is enforced by `throttle_stream` against a `ConnectionStreamCounter` that is scoped to a single `ConnectionTableKey` (IP + optional pubkey) — `SwQos::cache_new_connection` and `ConnectionTable::try_add_connection` reuse/share one `stream_counter` for all connections registered under the same key, but a *new, independent* counter is created for each distinct key [3](#0-2) . For unstaked (unauthenticated) peers there is no pubkey to bind, so the key is effectively per source IP, and `max_connections_per_unstaked_peer` only bounds concurrent connections *within* that one IP [4](#0-3) .

Because `on_new_stream` calls `throttle_stream` with this per-key counter and the fixed `max_unstaked_load_in_throttling_window` value [5](#0-4) , each distinct IP independently gets the full `MAX_UNSTAKED_TPS` (200 streams/sec) budget every `STREAM_THROTTLING_INTERVAL` (100ms) [6](#0-5) . Crucially, `increment_load` (used to drive `StakedStreamLoadEMA` and the `staked_throttling_enabled` trip) only accounts for staked traffic (`if peer_type.is_staked() { ... }`) [7](#0-6)  — unstaked load is never aggregated into any shared counter at all. The only ceiling on total distinct unstaked sources is `max_unstaked_connections` (default 2000) via connection-table pruning in `prune_unstaked_connection_table` [8](#0-7) , and the per-IP-per-minute new-connection rate limiter, which throttles the *rate of new connections* but not the sustained stream throughput of already-established connections [9](#0-8) .

Consequently, an attacker with N distinct unstaked source IPs (obtainable cheaply via NAT/IPv6 rotation or slow ramp-up respecting the per-minute connection limiter) can drive aggregate accepted unstaked stream throughput to roughly `N * MAX_UNSTAKED_TPS`, up to the `max_unstaked_connections` table-capacity limit, far exceeding the `EXPECTED_UNSTAKED_STREAMS_RATIO` (20%) share of `max_streams_per_ms` that the design comment implies should be the unstaked ceiling. This flood raises `load_in_recent_interval`/`current_load_ema` is not directly increased by unstaked traffic, but the sheer volume of accepted unstaked streams competes for the same connection-handling/packet-batch pipeline, degrading legitimate staked and unstaked traffic's TPU stream capacity.

### Impact Explanation
This is a QoS-evasion / resource-starvation issue in the TPU QUIC ingestion path: an unprivileged remote attacker can starve legitimate staked and unstaked senders of stream capacity by fanning out across many source IPs, none of which individually appear abusive to the per-key throttle. This falls under Agave's DoS/QoS-evasion bounty category for the streamer/QUIC ingress path.

### Likelihood Explanation
Feasible with only unprivileged network access: attacker needs many distinct source IPs/ports (via NAT pools, IPv6 address rotation, or cloud egress IP diversity), each opening ≤`max_connections_per_unstaked_peer` connections and respecting `max_connections_per_ipaddr_per_min` to avoid the connection-rate limiter. No staked identity, gossip presence, or validator control is required — this matches the "unstaked remote client hitting the public TPU port" attacker model. The only friction is acquiring enough distinct IPs and staying under `max_unstaked_connections` table capacity, both of which are practical for a moderately resourced attacker (e.g., cloud/NAT/IPv6 rotation), especially since the flood can be ramped up gradually.

### Recommendation
Replace or supplement the per-key (`ConnectionTableKey`) unstaked throttle with a global unstaked stream-rate budget shared across all unstaked connections/IPs — e.g., a shared `TokenBucket`/counter sized to `EXPECTED_UNSTAKED_STREAMS_RATIO * max_streams_per_ms`, consumed by every unstaked stream in `on_new_stream`/`throttle_stream`, independent of source IP. Additionally, feed unstaked stream counts into `StakedStreamLoadEMA`'s load accounting so `staked_throttling_enabled` and downstream stats correctly reflect aggregate unstaked pressure, not just staked load.

### Proof of Concept
Integration test plan (extending existing `streamer/src/nonblocking/quic.rs` / `swqos.rs` test harness, e.g. `spawn_stake_weighted_qos_server` and `setup_quic_server` helpers already used in `test_quic_server_multiple_connections_on_single_client_endpoint` [10](#0-9) ):

```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_unstaked_stream_throttle_scales_with_ip_count() {
    // Spawn server with SwQosConfig::default() (MAX_UNSTAKED_TPS=200/IP fixed budget)
    // and a small max_streams_per_ms to make the intended global unstaked share obvious.
    let SpawnTestServerResult { receiver, server_address, stats, cancel, .. } =
        setup_quic_server(None, QuicStreamerConfig::default_for_tests(),
            SwQosConfig { max_connections_per_unstaked_peer: 4, ..Default::default() });

    const NUM_SIMULATED_IPS: usize = 50; // bind sockets on distinct local ports/addrs to simulate distinct sources
    let mut handles = vec![];
    for i in 0..NUM_SIMULATED_IPS {
        handles.push(tokio::spawn(async move {
            // each "IP" opens up to max_connections_per_unstaked_peer connections
            // and sends streams at max rate for STREAM_THROTTLING_INTERVAL * 5
            open_unstaked_connections_and_flood(server_address, /*conns=*/4, Duration::from_millis(500)).await
        }));
    }
    for h in handles { h.await.unwrap(); }

    let accepted = count_received_packets_for(receiver, /*any size*/1, Duration::from_secs(1)).await;

    // Expected (bug): accepted grows ~linearly with NUM_SIMULATED_IPS, e.g. ~NUM_SIMULATED_IPS * 200 * 0.5 streams,
    // far exceeding EXPECTED_UNSTAKED_STREAMS_RATIO * max_streams_per_ms * elapsed.
    // Fixed behavior assertion: aggregate accepted stream count should stay bounded by
    // a global unstaked budget independent of NUM_SIMULATED_IPS, e.g.:
    let global_unstaked_budget = (EXPECTED_UNSTAKED_STREAMS_RATIO * DEFAULT_MAX_STREAMS_PER_MS as f64
        * 500.0 /* ms elapsed */) as usize;
    assert!(accepted <= global_unstaked_budget * 2 /* margin */,
        "aggregate unstaked throughput ({accepted}) scaled with IP count, exceeding intended global budget");

    cancel.cancel();
}
```

Run this with `NUM_SIMULATED_IPS` varied (e.g., 5, 50, 500) and assert accepted-stream counts do not scale proportionally with IP count — currently they do, because `available_load_capacity_in_throttling_duration` for `ConnectionPeerType::Unstaked` returns a fixed per-key value [11](#0-10)  rather than dividing a shared global budget across active unstaked sources.

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

**File:** streamer/src/nonblocking/quic.rs (L1392-1411)
```rust
    #[tokio::test(flavor = "multi_thread")]
    async fn test_quic_server_multiple_connections_on_single_client_endpoint() {
        agave_logger::setup();

        let SpawnTestServerResult {
            join_handle,
            receiver,
            server_address,
            stats,
            cancel,
        } = setup_quic_server(
            None,
            QuicStreamerConfig {
                ..QuicStreamerConfig::default_for_tests()
            },
            SwQosConfig {
                max_connections_per_unstaked_peer: 2,
                ..SwQosConfig::default_for_tests()
            },
        );
```

**File:** streamer/src/nonblocking/swqos.rs (L205-218)
```rust
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
```

**File:** streamer/src/nonblocking/swqos.rs (L241-256)
```rust
    fn prune_unstaked_connection_table(
        &self,
        unstaked_connection_table: &mut ConnectionTable<ConnectionStreamCounter>,
        max_unstaked_connections: usize,
        stats: Arc<StreamerStats>,
    ) {
        if unstaked_connection_table.total_size >= max_unstaked_connections {
            // Prune the connection table down to 90% capacity
            const PRUNE_TABLE_RATIO: f64 = 0.90;
            let max_connections = (PRUNE_TABLE_RATIO * (max_unstaked_connections as f64)) as usize;
            let num_pruned = unstaked_connection_table.prune_oldest(max_connections);
            stats
                .num_evictions_unstaked
                .fetch_add(num_pruned, Ordering::Relaxed);
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

**File:** streamer/src/quic.rs (L53-56)
```rust
/// The new connections per minute from a particular IP address.
/// Heuristically set to the default maximum concurrent connections
/// per IP address. Might be adjusted later.
pub const DEFAULT_MAX_CONNECTIONS_PER_IPADDR_PER_MINUTE: u64 = 8;
```

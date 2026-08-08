Based on the code I reviewed, this is a real, exploitable design property of the unstaked connection table.

### Title
Unprivileged attacker can churn unstaked QUIC connections to force `prune_oldest` eviction of legitimate unstaked senders - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
`SwQos::prune_unstaked_connection_table` triggers `ConnectionTable::prune_oldest` once `total_size >= max_unstaked_connections`, pruning whichever IP/pubkey bucket has the *lowest* `last_update` timestamp across its connections. An attacker who repeatedly opens fresh unstaked connections (each with a fresh `last_update = timestamp()`) from many source IPs/ports keeps their own entries' `last_update` newer than idle legitimate connections, causing legitimate connections to be selected for eviction instead of the attacker's own churn.

### Finding Description
`prune_unstaked_connection_table` (streamer/src/nonblocking/swqos.rs:241-256) prunes down to 90% of `max_unstaked_connections` by calling `ConnectionTable::prune_oldest` (streamer/src/nonblocking/quic.rs:964-980), which picks the table bucket whose *minimum* `last_update` among its `ConnectionEntry`s is smallest, and evicts the entire bucket.

`last_update` is set to `timing::timestamp()` at connection-context creation time in `SwQos::build_connection_context` (streamer/src/nonblocking/swqos.rs:301-342), and is keyed per-IP (or per-pubkey if a client cert pubkey is present) via `ConnectionTableKey`. Because the eviction key is "oldest `last_update`" rather than any measure of activity/fairness or per-source rate, a client that establishes new connections from many distinct IPs/ports keeps generating buckets with brand-new (large) `last_update` values. Meanwhile, legitimate unstaked senders that are idle (e.g., between transaction bursts, or slow to reconnect) sit with older `last_update` values and are the ones repeatedly selected by `min_by_key` for `swap_remove_index`, being evicted first as the table crosses the 90% threshold in `prune_unstaked_connections_and_add_new_connection` (streamer/src/nonblocking/swqos.rs:258-290).

There is no rate limiting on *new-connection establishment* itself (as opposed to per-connection stream/tx throughput, which is throttled separately via `StakedStreamLoadEMA`), and no per-source-IP cap on how many distinct buckets an attacker can create (each new IP is a new table key, `max_connections_per_unstaked_peer` only bounds connections *within* one key). This means a distributed/many-port attacker can generate churn faster than legitimate senders' natural reconnect cadence, keeping their own entries "freshest" and starving others of unstaked TPU connection slots.

### Impact Explanation
This is a QoS-evasion / fairness violation: an unprivileged, unstaked attacker can unfairly capture unstaked TPU connection capacity, degrading connectivity for legitimate unstaked transaction senders. It does not cause a memory-safety or consensus issue, but it matches the "grossly underpriced pre-fee work / QoS evasion" bounty category — the attacker pays essentially nothing (no stake, cheap QUIC handshakes) to deny bandwidth to others sharing the unstaked pool.

### Likelihood Explanation
Preconditions are modest: the attacker needs to be able to open many QUIC connections from distinct source IPs/ports (or repeatedly reconnect) faster than legitimate clients naturally refresh their connections, and sustain this longer than legitimate senders' idle periods. This is feasible from a single machine with IP variety (e.g., cloud provider with many addresses) or many ephemeral local ports for the per-pubkey keyed case. The `prune_oldest` selection purely by timestamp recency, with no source-rate accounting, makes the attack straightforward to reproduce deterministically in a unit test using `ConnectionTable::try_add_connection` and `prune_oldest` directly.

### Recommendation
Change `prune_oldest`'s selection/admission policy so that connection recency alone cannot be gamed by churn: e.g., track per-source-IP/subnet connection creation rate and reject or throttle admission of new unstaked connections from sources that create-and-drop connections above some rate, or weight eviction by a combination of "connection age since first-seen" plus stream activity rather than solely the most recent `last_update` refresh, or maintain a minimum grace/dwell time before a freshly-added connection is eligible to "protect" its bucket from eviction priority. Alternatively, cap distinct unstaked buckets per subnet/CIDR to blunt multi-IP churn.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (test module)
#[tokio::test]
async fn test_prune_oldest_churn_evicts_legitimate_idle_connections() {
    use std::net::Ipv4Addr;
    let cancel = CancellationToken::new();
    let mut table = ConnectionTable::new(ConnectionTableType::Unstaked, cancel);
    let stats = Arc::new(StreamerStats::default());
    let max_connections_per_peer = 10;

    // N legitimate idle unstaked connections, added first (old last_update)
    let legit_ips: Vec<_> = (0..5)
        .map(|i| IpAddr::V4(Ipv4Addr::new(10, 0, 0, i)))
        .collect();
    for (i, ip) in legit_ips.iter().enumerate() {
        table.try_add_connection(
            ConnectionTableKey::IP(*ip), 0,
            ClientConnectionTracker::new(stats.clone(), 1000).unwrap(),
            None, ConnectionPeerType::Unstaked,
            Arc::new(AtomicU64::new(i as u64)), // old timestamps: 0..5
            max_connections_per_peer, || Arc::new(NullStreamerCounter {}),
        ).unwrap();
    }

    // M attacker connections from distinct IPs, continuously refreshed (newer last_update)
    for j in 0..20 {
        let attacker_ip = IpAddr::V4(Ipv4Addr::new(20, 0, 0, (j % 255) as u8));
        table.try_add_connection(
            ConnectionTableKey::IP(attacker_ip), 0,
            ClientConnectionTracker::new(stats.clone(), 1000).unwrap(),
            None, ConnectionPeerType::Unstaked,
            Arc::new(AtomicU64::new(1000 + j as u64)), // always newer than legit
            max_connections_per_peer, || Arc::new(NullStreamerCounter {}),
        ).unwrap();
    }

    // Trigger pruning down to fewer entries than legit + attacker combined
    let pruned = table.prune_oldest(legit_ips.len());
    assert!(pruned > 0);

    // Assert legitimate connections were evicted despite being "legitimate", not attacker's
    for ip in &legit_ips {
        assert!(
            !table.table.contains_key(&ConnectionTableKey::IP(*ip)),
            "expected legitimate idle connection to be pruned due to churn"
        );
    }
    // Attacker-controlled fresh entries survive
    assert!(table.total_size > 0);
}
```
Expected result: the test demonstrates that all legitimate idle entries are pruned in favor of the attacker's continuously-refreshed entries, confirming the churn-based eviction bypass. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** streamer/src/nonblocking/quic.rs (L964-980)
```rust
    pub(crate) fn prune_oldest(&mut self, max_size: usize) -> usize {
        let mut num_pruned = 0;
        let key = |(_, connections): &(_, &Vec<_>)| {
            connections.iter().map(ConnectionEntry::last_update).min()
        };
        while self.total_size.saturating_sub(num_pruned) > max_size {
            match self.table.values().enumerate().min_by_key(key) {
                None => break,
                Some((index, connections)) => {
                    num_pruned += connections.len();
                    self.table.swap_remove_index(index);
                }
            }
        }
        self.total_size = self.total_size.saturating_sub(num_pruned);
        num_pruned
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L241-290)
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

    async fn prune_unstaked_connections_and_add_new_connection(
        &self,
        client_connection_tracker: ClientConnectionTracker,
        connection: &Connection,
        connection_table: Arc<Mutex<ConnectionTable<ConnectionStreamCounter>>>,
        max_connections: usize,
        conn_context: &SwQosConnectionContext,
    ) -> Result<
        (
            Arc<AtomicU64>,
            CancellationToken,
            Arc<ConnectionStreamCounter>,
        ),
        ConnectionHandlerError,
    > {
        let stats = self.stats.clone();
        if max_connections > 0 {
            let mut connection_table = connection_table.lock().await;
            self.prune_unstaked_connection_table(&mut connection_table, max_connections, stats);
            self.cache_new_connection(
                client_connection_tracker,
                connection,
                connection_table,
                conn_context,
            )
        } else {
            connection.close(
                CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                CONNECTION_CLOSE_REASON_DISALLOWED,
            );
            Err(ConnectionHandlerError::ConnectionAddError)
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L301-342)
```rust
impl QosController<SwQosConnectionContext> for SwQos {
    fn build_connection_context(&self, connection: &Connection) -> SwQosConnectionContext {
        let remote_address = connection.remote_address();
        get_connection_stake(connection, &self.staked_nodes).map_or(
            SwQosConnectionContext {
                peer_type: ConnectionPeerType::Unstaked,
                total_stake: 0,
                remote_pubkey: None,
                in_staked_table: false,
                remote_address,
                stream_counter: None,
                last_update: Arc::new(AtomicU64::new(timing::timestamp())),
            },
            |(pubkey, stake, total_stake)| {
                // The heuristic is that the stake should be large enough to have 1 stream pass through within one throttle
                // interval during which we allow max (MAX_STREAMS_PER_MS * STREAM_THROTTLING_INTERVAL_MS) streams.

                let peer_type = {
                    let max_streams_per_ms = self.staked_stream_load_ema.max_streams_per_ms();
                    let min_stake_ratio =
                        1_f64 / (max_streams_per_ms * STREAM_THROTTLING_INTERVAL_MS) as f64;
                    let stake_ratio = stake as f64 / total_stake as f64;
                    if stake_ratio < min_stake_ratio {
                        // If it is a staked connection with ultra low stake ratio, treat it as unstaked.
                        ConnectionPeerType::Unstaked
                    } else {
                        ConnectionPeerType::Staked(stake)
                    }
                };

                SwQosConnectionContext {
                    peer_type,
                    total_stake,
                    remote_pubkey: Some(pubkey),
                    in_staked_table: false,
                    remote_address,
                    last_update: Arc::new(AtomicU64::new(timing::timestamp())),
                    stream_counter: None,
                }
            },
        )
    }
```

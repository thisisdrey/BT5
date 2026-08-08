## Finding

### Title
Unstaked QUIC connections can refresh `last_update` via trivial/cheap streams to evade `prune_oldest` eviction and deny slots to other unstaked peers - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The QUIC unstaked-connection table evicts connections purely by "oldest `last_update` timestamp" when the table is over capacity, and that timestamp is refreshed on every completed stream regardless of the stream's content, cost, or usefulness. An unprivileged, unstaked client can hold several connections and keep issuing trivial/near-free streams to continuously refresh `last_update`, guaranteeing its own connections are never the "oldest" and are therefore never pruned, while other unstaked peers that are momentarily less active get evicted first. This is the same bug class as the Nouns DAO report: a shared, capacity-limited resource is bounded by an unvalidated/attacker-controlled marker (a "last activity" timestamp here, "last proposal" there), and cheap attacker-generated activity is used to bias that marker in the attacker's favor at the expense of other legitimate, unprivileged participants.

### Finding Description
`ConnectionTable::prune_oldest` selects connections to evict strictly by the minimum `last_update` value across all tracked connections: [1](#0-0) 

`last_update` is a shared `AtomicU64` handed to each `ConnectionEntry` at connection-add time, and is refreshed on `on_stream_finished` — an event fired for every completed stream, independent of whether the stream carried any useful transaction, and independent of stake or fee: [2](#0-1) [3](#0-2) 

For unstaked peers, when the unstaked connection table nears capacity, `prune_unstaked_connection_table` prunes down to 90% capacity using exactly this oldest-`last_update` heuristic — there is no fee, stake, or legitimacy check involved for the unstaked path: [4](#0-3) 

This pruning function is invoked both from the staked fallback path and directly from the unstaked `try_add_connection` branch whenever a new unstaked connection needs room: [5](#0-4) [6](#0-5) 

Because `max_connections_per_unstaked_peer` allows more than one connection per unstaked IP/pubkey, and each connection independently refreshes its own `last_update` on every finished stream, an attacker can hold multiple unstaked connections and keep them artificially "fresh" by sending trivial streams (e.g., near-empty payload streams) at negligible bandwidth/CPU cost — no signature verification cost, no fee, no stake required. Any other unstaked peer that pauses activity for even a short interval (e.g., between legitimate transaction submissions) becomes the pruning target instead, even though its activity is otherwise entirely legitimate.

### Impact Explanation
This lets an unprivileged (unstaked, no-fee) attacker systematically win the "who gets pruned" decision in the unstaked connection table, denying other unstaked clients — including legitimate low-stake or new users who must use the unstaked path — connection slots to submit transactions. This is a QoS evasion: the attacker converts a small, continuous stream of cheap/no-op activity into a durable claim on a scarce, shared network resource (unstaked connection capacity), which the streamer explicitly caps for fairness (`max_unstaked_connections`, `max_connections_per_unstaked_peer`). It does not require any deposit, fee, or stake, matching the "grossly underpriced pre-fee work" / "QoS evasion" impact bar.

### Likelihood Explanation
The attack requires no privileges: any unstaked client can open the maximum allowed connections per unstaked peer and keep issuing trivial streams on a timer faster than the node's pruning check interval. It is deterministic — `prune_oldest`/`prune_unstaked_connection_table` always evict by the same single criterion (oldest `last_update`), with no randomization or legitimacy weighting for the unstaked table (unlike the staked table's `prune_random` with stake threshold). The cost is only ongoing minimal QUIC stream traffic, comparable to the "gas for a bogus proposal" cost in the original report.

### Recommendation
For the unstaked connection table, avoid basing eviction purely on a freely-refreshable "last completed stream" timestamp. Consider incorporating per-connection request legitimacy (e.g., number of valid, sigverified transactions actually forwarded) into the pruning score, or fall back to `prune_random` (as already used for the staked table) instead of pure oldest-timestamp eviction for the unstaked table, so that trivial/no-op streams cannot be used to indefinitely evade eviction.

### Proof of Concept
1. Attacker (no stake required) opens `max_connections_per_unstaked_peer` QUIC connections to the validator's TPU.
2. On each connection, attacker periodically opens and immediately finishes a minimal/near-empty stream — cheap for the attacker, and each completion calls `on_stream_finished`, refreshing that connection's `last_update` via `swqos.rs:490-494`.
3. When the unstaked table approaches `max_unstaked_connections`, any new unstaked connection triggers `prune_unstaked_connection_table` (`swqos.rs:241-256`), which calls `ConnectionTable::prune_oldest` (`quic.rs:964-980`) and evicts whichever tracked connections currently have the smallest `last_update` value.
4. Because the attacker's connections are continuously refreshed, they are never the minimum; other unstaked peers whose connections have gone briefly idle (e.g., a legitimate wallet between retries) are evicted instead, denying them the ability to submit transactions through the unstaked path.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L860-902)
```rust
struct ConnectionEntry<S: OpaqueStreamerCounter> {
    cancel: CancellationToken,
    peer_type: ConnectionPeerType,
    last_update: Arc<AtomicU64>,
    port: u16,
    // We do not explicitly use it, but its drop is triggered when ConnectionEntry is dropped.
    _client_connection_tracker: ClientConnectionTracker,
    connection: Option<Connection>,
    stream_counter: Arc<S>,
}

impl<S: OpaqueStreamerCounter> ConnectionEntry<S> {
    fn new(
        cancel: CancellationToken,
        peer_type: ConnectionPeerType,
        last_update: Arc<AtomicU64>,
        port: u16,
        client_connection_tracker: ClientConnectionTracker,
        connection: Option<Connection>,
        stream_counter: Arc<S>,
    ) -> Self {
        Self {
            cancel,
            peer_type,
            last_update,
            port,
            _client_connection_tracker: client_connection_tracker,
            connection,
            stream_counter,
        }
    }

    fn last_update(&self) -> u64 {
        self.last_update.load(Ordering::Relaxed)
    }

    fn stake(&self) -> u64 {
        match self.peer_type {
            ConnectionPeerType::Unstaked => 0,
            ConnectionPeerType::Staked(stake) => stake,
        }
    }
}
```

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

**File:** streamer/src/nonblocking/swqos.rs (L258-289)
```rust
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
```

**File:** streamer/src/nonblocking/swqos.rs (L415-437)
```rust
                ConnectionPeerType::Unstaked => {
                    if let Ok((last_update, cancel_connection, stream_counter)) = self
                        .prune_unstaked_connections_and_add_new_connection(
                            client_connection_tracker,
                            connection,
                            self.unstaked_connection_table.clone(),
                            self.config.max_unstaked_connections,
                            conn_context,
                        )
                        .await
                    {
                        self.stats
                            .connection_added_from_unstaked_peer
                            .fetch_add(1, Ordering::Relaxed);
                        conn_context.in_staked_table = false;
                        conn_context.last_update = last_update;
                        conn_context.stream_counter = Some(stream_counter);
                        return Some(cancel_connection);
                    } else {
                        self.stats
                            .connection_add_failed_unstaked_node
                            .fetch_add(1, Ordering::Relaxed);
                    }
```

**File:** streamer/src/nonblocking/swqos.rs (L490-494)
```rust
    fn on_stream_finished(&self, context: &SwQosConnectionContext) {
        context
            .last_update
            .store(timing::timestamp(), Ordering::Relaxed);
    }
```

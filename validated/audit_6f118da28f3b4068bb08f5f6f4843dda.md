Confirmed: `remove_connection` fully evicts the `IndexMap` entry once its `Vec` of `ConnectionEntry` becomes empty [1](#0-0) , and `try_add_connection` only reuses an existing `stream_counter`/`TokenBucket` when the entry for that key already contains at least one live connection — otherwise it manufactures a brand-new counter via `stream_counter_factory` with a full quota [2](#0-1) .

### Title
Stake-weighted QUIC stream throttling is reset by disconnect/reconnect, allowing unstaked/staked clients to evade per-connection rate limits - (File: streamer/src/nonblocking/quic.rs, streamer/src/nonblocking/simple_qos.rs, streamer/src/nonblocking/swqos.rs)

### Summary
The QUIC streamer's per-connection rate-limiting state (`TokenBucket` in `SimpleQos`, `ConnectionStreamCounter` in `SwQos`) is keyed to a `ConnectionEntry` living inside `ConnectionTable`. This state is supposed to persist and accumulate usage for a given peer (by pubkey or IP) so that stake-weighted stream/transaction throttling limits are enforced over time. However, once all connections for a key are closed, `ConnectionTable::remove_connection` swap-removes the entire map entry [3](#0-2) . Any unprivileged client can therefore reset its own throttling state at will simply by closing and re-opening its QUIC connection, since `try_add_connection` allocates a brand-new, fully-refilled counter whenever no existing entry is found for the key [2](#0-1) .

### Finding Description
This is the closest reachable analog to the reported bug class: a mechanism intended to enforce a persistent, monotonic restriction (rate-limit accounting analogous to the vault's "irreversible" emergency-exit accounting) can be trivially reset/reversed by an ordinary protocol participant performing a legitimate-looking action (closing and reopening a connection), rather than by the mechanism that is supposed to control it.

Specifically:
- `SimpleQos::cache_new_connection` builds a `TokenBucket` with `max_streams_per_second` capacity only the first time a peer's key is inserted; it is retrieved/shared from the first live `ConnectionEntry` for that key on subsequent connections [4](#0-3) .
- Likewise `SwQos::cache_new_connection` reuses `ConnectionStreamCounter` only via the same `try_add_connection` reuse-or-create logic [5](#0-4) .
- Once a peer disconnects (either by choice, natural timeout, or triggering the peer's own connection to be dropped/pruned) and the connection count for that key hits zero, the underlying `IndexMap` entry is removed entirely, along with its associated rate-limiting counter [6](#0-5) .
- Reconnecting (even with the same pubkey and IP) causes `entry(key).or_default()` to start empty, so the very next `try_add_connection` call manufactures a brand new `TokenBucket`/`ConnectionStreamCounter` with a completely full quota [2](#0-1) .

Because a QUIC client fully controls when its own connection closes, this is directly reachable by any unprivileged, unstaked or low-stake peer without needing validator/operator privileges: repeatedly opening and forcibly closing connections to the TPU/TPU-forwards QUIC endpoint allows a client to reset its throttle state indefinitely.

### Impact Explanation
This maps to the "QoS evasion" impact category explicitly allowed by the assessment rules. The stake-weighted throttling in `SwQos`/`SimpleQos` is the validator's core defense for pre-fee-market ingestion of transactions from staked/unstaked peers — it is what prevents a single peer from monopolizing the QUIC-stream (and therefore SigVerify/Banking-stage) pipeline relative to its stake weight. If a peer can reset its counter for free by dropping and re-establishing a QUIC connection, it can send far more streams per unit time than its stake should entitle it to, degrading service for legitimately-throttled higher-stake peers and increasing unprefixed/pre-fee CPU and packet-processing load on the node.

### Likelihood Explanation
Likelihood is high in terms of reachability — any client, staked or unstaked, controls its own QUIC connection lifecycle and can trivially close and re-open connections. The reconnection itself does incur the (cheap) cost of a new QUIC/TLS handshake, which provides some natural friction, but this is far cheaper than the throughput gained by resetting a stake-weighted throttle window, and does not require elevated privileges, cluster gossip weight, or any config the operator controls at bootstrap.

### Recommendation
Rate-limiting/token-bucket state for a given key (pubkey preferably, since IP is spoof-adjacent) should not be destroyed merely because the connection count for that key transiently reaches zero. Consider:
- Decoupling the throttling counter's lifetime from `ConnectionEntry` lifetime — e.g., keep a separate LRU/expiring map of `(key -> TokenBucket/ConnectionStreamCounter)` (similar to the existing `net-utils::token_bucket` LRU eviction machinery already used elsewhere) that only expires counters after a cooldown window rather than immediately on disconnect.
- Alternatively, retain the last-known token count when swap-removing the empty `Vec` in `remove_connection`/`remove_connections_by_key`, and restore it (partially decayed by elapsed time) when a new connection for the same key is established.

### Proof of Concept
Conceptually (against `streamer/src/nonblocking/quic.rs` + `simple_qos.rs`/`swqos.rs`):
1. Staked/unstaked client opens a QUIC connection and rapidly opens streams until `stream_counter.consume_tokens(1)` starts failing (i.e., the peer is throttled per `on_new_stream`) [7](#0-6) .
2. Client immediately closes the connection. `remove_connection`/`remove_connections_by_key` swap-removes the now-empty entry for the client's key, discarding the `TokenBucket` state [8](#0-7) .
3. Client reconnects with the same identity/IP. `try_add_connection` finds no existing entry, so `stream_counter_factory` allocates a fresh, fully-topped-off `TokenBucket`/`ConnectionStreamCounter` [2](#0-1) .
4. Repeat steps 1–3 in a tight loop to sustain a stream/transaction submission rate well above what the peer's stake should permit under `available_load_capacity_in_throttling_duration` [9](#0-8) , evading the intended stake-weighted QoS ceiling.

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

**File:** streamer/src/nonblocking/quic.rs (L1077-1108)
```rust
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

    /// Removes all connections associated with `key`.
    ///
    /// Returns the number of removed connections.
    pub(crate) fn remove_connections_by_key(&mut self, key: ConnectionTableKey) -> usize {
        self.table
            .swap_remove(&key)
            .map(|connections| {
                let num_removed = connections.len();
                debug_assert!(
                    self.total_size >= num_removed,
                    "connection table size underflow while removing by key; total_size={}, \
                     removed={}",
                    self.total_size,
                    num_removed
                );
                self.total_size = self.total_size.saturating_sub(num_removed);
                num_removed
            })
            .unwrap_or_default()
    }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L206-223)
```rust
        let key = ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey);
        if let Some((last_update, cancel_connection, stream_counter)) = connection_table_l
            .try_add_connection(
                key,
                remote_addr.port(),
                client_connection_tracker,
                Some(connection.clone()),
                conn_context.peer_type(),
                conn_context.last_update.clone(),
                self.config.max_connections_per_peer,
                || {
                    Arc::new(TokenBucket::new(
                        self.config.max_streams_per_second,
                        self.config.max_streams_per_second,
                        self.config.max_streams_per_second as f64,
                    ))
                },
            )
```

**File:** streamer/src/nonblocking/simple_qos.rs (L393-418)
```rust
            let stream_counter = context
                .stream_counter
                .as_ref()
                .expect("This will always be populated before streams are opened");

            while stream_counter.consume_tokens(1).is_err() {
                debug!("Throttling stream from {remote_addr:?}");
                self.stats.throttled_streams.fetch_add(1, Ordering::Relaxed);
                match peer_type {
                    ConnectionPeerType::Unstaked => {
                        self.stats
                            .throttled_unstaked_streams
                            .fetch_add(1, Ordering::Relaxed);
                    }
                    ConnectionPeerType::Staked(_) => {
                        self.stats
                            .throttled_staked_streams
                            .fetch_add(1, Ordering::Relaxed);
                    }
                }
                let min_sleep = stream_counter.us_to_have_tokens(1).expect(
                    "Valid QoS configurations guarantee enough token bucket fits at least one \
                     token",
                );
                sleep(Duration::from_micros(min_sleep)).await;
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

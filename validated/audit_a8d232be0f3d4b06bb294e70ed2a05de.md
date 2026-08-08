### Title
QUIC connection eviction timer can be self-refreshed to evade pruning under connection-table pressure - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The reported StakedCap bug class is: an unprivileged, permissionless action lets an actor repeatedly reset a shared timer that gates a scarce resource, letting the actor indefinitely dodge the mechanism meant to fairly reclaim that resource, at the expense of other legitimate participants. In the Agave TPU QUIC streamer, `ConnectionTable::prune_oldest` reclaims connection-table capacity by evicting the entry with the *oldest* `last_update` timestamp [1](#0-0) . That timestamp is stored in an `Arc<AtomicU64>` per connection and is refreshed on every completed stream via `on_stream_finished`, which any connected client fully controls by opening trivial unidirectional streams [2](#0-1) [3](#0-2) .

### Finding Description
`ConnectionEntry.last_update` is initialized to the connection's creation timestamp and only ever moves forward through `on_stream_finished`, which fires each time a stream is fully consumed (i.e. `handle_chunks` returns `Ok(StreamState::Finished)` after any 0+ byte stream completes) [4](#0-3) . `prune_unstaked_connection_table`/`prune_oldest` is invoked whenever the unstaked connection table is at or above capacity, and it evicts whichever IP/pubkey bucket currently has the smallest (i.e., stalest) `last_update` value across the table [5](#0-4) [1](#0-0) .

Because opening and completing a minimal QUIC uni-stream is essentially free (subject only to the existing per-connection/per-IP stream-rate throttling, not to the pruning logic itself), any unprivileged peer holding a connection can perpetually "touch" its own `last_update` field just by trickling near-empty streams through the connection. This guarantees that peer's connection entry is never the "oldest" candidate for `prune_oldest`, so when the table is under capacity pressure, other connections that are quieter (e.g., a legitimate client only submitting real transactions sporadically) are preferentially evicted instead. The staked-connection path (`prune_random`) is stake-weighted and not affected the same way, but the unstaked-connection eviction path relies solely on this self-reported/self-refreshable timestamp, with no floor on how cheaply or how frequently it can be refreshed.

This mirrors the report's bug class precisely: a cheap, permissionless action (`notify()` in the original report vs. opening a trivial stream here) resets a timer (`lastNotify` vs. `last_update`) that gates a fairness/eviction mechanism (`lockedProfit` vesting vs. LRU-based connection pruning), letting the actor indefinitely evade the intended reclaiming behavior at the expense of other participants sharing the same resource pool.

### Impact Explanation
This is a QoS-evasion vector on the TPU QUIC unstaked-connection table: an attacker can occupy connection slots indefinitely by minimally refreshing its own eviction timestamp, causing `prune_oldest` to repeatedly select and evict other (potentially legitimate) unstaked connections instead of the attacker's own idle-but-refreshed connection. This does not cause a validator panic, deadlock, unbounded memory growth, consensus/verification bypass, or invalid recorded blocks — it is purely a fairness/QoS-evasion issue in the unstaked connection admission/eviction path, capped by the existing `max_connections_per_unstaked_peer` and `max_unstaked_connections` limits.

### Likelihood Explanation
Likelihood is limited: the impact is bounded by existing per-peer connection caps and per-connection stream-rate throttling (`TokenBucket`/`throttle_stream`), and unstaked connections are inherently lower priority/lower trust already. The effect is a shift in which unstaked connections get evicted first under contention, not a global resource-exhaustion or protocol-level failure. It is a plausible but low-severity fairness quirk rather than a high-impact node-level DoS.

### Recommendation
Consider decoupling "eviction candidacy" from a value that is entirely and cheaply self-refreshable by the connection owner — e.g., base pruning partly on total streams/bytes actually useful to the node (real transaction throughput) rather than mere last-activity time, or add a minimum dwell/no-refresh-below-threshold window analogous to the report's short-term recommendation of not allowing the "vesting timer" to be reset until a minimum period has elapsed. This would prevent a peer from indefinitely avoiding LRU-based eviction using only trivial/near-empty streams.

### Proof of Concept
Not independently reproduced beyond static code review; the mechanism is confirmed by reading the cited functions (`on_stream_finished`, `prune_oldest`, `prune_unstaked_connection_table`, `handle_chunks`). Exploitability would require setting up a QUIC client that repeatedly opens and immediately closes near-empty unidirectional streams against `TPU` while the unstaked connection table is near capacity, and observing that this connection's table entry is never selected by `prune_oldest`, while other connections are evicted first — this dynamic verification was not performed within this analysis.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L768-857)
```rust
    // n_chunks == 0 marks the end of a stream
    if n_chunks != 0 {
        return Ok(StreamState::Receiving);
    }

    if accum.chunks.is_empty() {
        debug!("stream is empty");
        stats
            .total_packet_batches_none
            .fetch_add(1, Ordering::Relaxed);
        return Err(());
    }

    // done receiving chunks
    let bytes_sent = accum.meta.size;

    // 86% of transactions/packets come in one chunk. In that case,
    // we can just move the chunk to the `Packet` and no copy is
    // made.
    // 14% of them come in multiple chunks. In that case, we copy
    // them into one `Bytes` buffer. We make a copy once, with
    // intention to not do it again.
    let packet = if accum.chunks.len() == 1 {
        BytesPacket::new(
            accum.chunks.pop().expect("expected one chunk"),
            accum.meta.clone(),
        )
    } else {
        let mut buf = BytesMut::with_capacity(bytes_sent);
        for chunk in &accum.chunks {
            buf.put_slice(chunk);
        }
        BytesPacket::new(buf.freeze(), accum.meta.clone())
    };

    let packet_size = packet.meta().size;
    let total_latency = accum.start_time.elapsed();
    if total_latency > rtt.mul_f32(LATE_REASSEMBLY_THRESHOLD) {
        debug!("Stream reassembly dealyed {}", total_latency.as_millis());
        stats
            .reassembly_delayed_streams
            .fetch_add(1, Ordering::Relaxed);
        stats
            .reassembly_delayed_streams_cumulative_delay_us
            .fetch_add(total_latency.as_micros() as usize, Ordering::Relaxed);
    }
    let packet_batch = PacketBatch::Single(packet);

    if let Err(err) = packet_sender.try_send(packet_batch) {
        stats
            .total_handle_chunk_to_packet_send_err
            .fetch_add(1, Ordering::Relaxed);
        match err {
            TrySendError::Full(_) => {
                stats
                    .total_handle_chunk_to_packet_send_full_err
                    .fetch_add(1, Ordering::Relaxed);
            }
            TrySendError::Disconnected(_) => {
                stats
                    .total_handle_chunk_to_packet_send_disconnected_err
                    .fetch_add(1, Ordering::Relaxed);
            }
        }
        trace!("packet batch send error {err:?}");
    } else {
        stats
            .total_bytes_sent_to_consumer
            .fetch_add(packet_size, Ordering::Relaxed);
        stats
            .total_packets_sent_to_consumer
            .fetch_add(1, Ordering::Relaxed);

        match peer_type {
            ConnectionPeerType::Unstaked => {
                stats
                    .total_unstaked_packets_sent_for_batching
                    .fetch_add(1, Ordering::Relaxed);
            }
            ConnectionPeerType::Staked(_) => {
                stats
                    .total_staked_packets_sent_for_batching
                    .fetch_add(1, Ordering::Relaxed);
            }
        }

        trace!("sent {bytes_sent} byte packet for batching");
    }

    Ok(StreamState::Finished)
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

**File:** streamer/src/nonblocking/swqos.rs (L490-494)
```rust
    fn on_stream_finished(&self, context: &SwQosConnectionContext) {
        context
            .last_update
            .store(timing::timestamp(), Ordering::Relaxed);
    }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L379-383)
```rust
    fn on_stream_finished(&self, context: &SimpleQosConnectionContext) {
        context
            .last_update
            .store(timing::timestamp(), Ordering::Relaxed);
    }
```

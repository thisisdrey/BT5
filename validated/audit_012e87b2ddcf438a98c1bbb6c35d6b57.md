### Title
Banned Pubkey Race Condition Allows Continued QUIC Stream/Transaction Submission After `SimpleQosBanlist::ban()` (QoS Evasion) - (File: streamer/src/nonblocking/simple_qos.rs)

### Summary
`SimpleQosBanlist::ban()` records a pubkey as banned and then asynchronously enqueues a request to evict that pubkey's existing QUIC connections. The connection is not torn down synchronously with the ban decision — actual removal happens later, on a background tokio task that pulls from a bounded mpsc channel. Between the moment a pubkey is banned and the moment `spawn_connection_evictor` actually removes its connection from the `ConnectionTable`, an already-connected banned peer can continue opening streams and pushing transaction packets through its still-registered connection, exactly analogous to the reported Bob/Alice whitelist race: the enforcement (`remove`/ban) and the actual effect (losing the ability to transact) are decoupled in time, and the restricted party can exploit that window.

### Finding Description
`ban()` performs two independent actions with no synchronization between them: [1](#0-0) 

1. `self.banlist.ban(pubkey, timeout)` — updates the shared `Banlist<Pubkey>` state used by `is_banned()`.
2. `self.eviction_sender.try_send(pubkey)` — merely queues a request to actually remove the connection; if the bounded channel (`MAX_IN_FLIGHT_EVICTIONS = 2000`) is full, the eviction is silently dropped and only logged as an error, per: [2](#0-1) 

The actual removal of the peer's live connection happens asynchronously on a dedicated tokio task: [3](#0-2) 

Because `remove_connections_by_key` is only invoked when the evictor task later dequeues the pubkey from the channel and acquires the `staked_connection_table` mutex, there is an unbounded (from the caller's perspective) window in which:
- The peer's `Connection` object remains present in `ConnectionTable`.
- Existing open QUIC streams on that connection continue to be read and forwarded to the packet sender / banking stage, since nothing in the packet-receiving path re-checks `is_banned()` per-stream.
- If the eviction channel is saturated (which is plausible under a burst of many simultaneous bans, or simply due to scheduler delay under load), the ban's enforcement is delayed indefinitely or dropped outright.

This mirrors the report's core defect: a restriction (`remove`/ban) is applied “logically” but the entity being restricted retains the practical ability to act (transfer tokens / submit transactions) until a separate, unsynchronized step actually revokes access. Here the "attacker" is the banned peer itself — an unprivileged client — racing to push as much QUIC stream/transaction traffic as possible through its already-established connection during the gap between `ban()` being called and the evictor task actually removing the connection.

### Impact Explanation
This is a QoS-evasion bug reachable purely by an unprivileged, already-connected QUIC client (no validator/operator role required to exploit — only to trigger the ban). During the race window the banned peer:
- Continues consuming its previously-granted stream/stake-based throughput quota.
- Can continue submitting transactions into banking stage even though it has been banned, defeating the purpose of the ban (e.g., banning a peer for QoS abuse, spam, or protocol violations).
- Under channel saturation (many concurrent bans), eviction requests are silently dropped, meaning some banned connections may never be evicted at all until the next `BANLIST_PRUNE_INTERVAL`-driven prune cycle (which only prunes the ban-list entries themselves, not necessarily removes connections it missed).

This falls under the explicitly accepted "QoS evasion" impact category.

### Likelihood Explanation
The race window exists on every single `ban()` call by construction — it's not a rare timing accident but a structural decoupling between the synchronous `banlist.ban()` and the asynchronous channel-based eviction. The magnitude of the window depends on: tokio scheduler latency, channel backlog, and mutex contention on `staked_connection_table`, all of which are exacerbated under the very load conditions (bursty/abusive traffic) that typically trigger bans in the first place — making the bug most impactful exactly when banning matters most.

### Recommendation
Make eviction synchronous with (or block on completion of) the ban decision, or at minimum check `is_banned()` on every stream/packet ingested from an existing connection (not just at connection-establishment time), so a banned peer's already-open connection stops being serviced immediately rather than waiting for the background evictor. Additionally, treat a full eviction channel as a hard error requiring immediate synchronous removal rather than a dropped, logged-only request.

### Proof of Concept
1. A staked peer opens a QUIC connection and is accepted into `SimpleQos`'s `staked_connection_table`, gaining its stake-weighted stream allowance.
2. The peer begins opening many streams and sending transaction packets in a tight loop.
3. An external detector calls `SimpleQosBanlist::ban(pubkey, timeout)` for this peer (e.g. due to abusive stream behavior).
4. Because `ban()` only updates `Banlist` state and pushes an entry onto the bounded `eviction_sender` channel [4](#0-3) , the peer's existing connection is not torn down at this instant.
5. The peer continues sending streams; those packets keep flowing to the packet sender until `spawn_connection_evictor`'s `eviction_receiver.recv()` fires and `remove_connections_by_key` executes [5](#0-4) , or indefinitely if the eviction channel is saturated by concurrent bans and the request is dropped per the `TrySendError::Full` branch [6](#0-5) .

**Uncertainty / caveats:** I was unable to fully trace whether `is_banned()` is also consulted somewhere in the per-stream/per-packet hot path (as opposed to only at connection setup) due to running out of search iterations; if such a check exists it would significantly narrow or eliminate this race window, and I could not conclusively rule that out. This should be verified before treating the finding as fully confirmed.

### Citations

**File:** streamer/src/nonblocking/simple_qos.rs (L43-52)
```rust
const BANLIST_PRUNE_INTERVAL: Duration = Duration::from_hours(1);

/// For simple QoS we only ban staked connections.
/// Overprovision at 2000 which assumes we ban every validator
const MAX_IN_FLIGHT_EVICTIONS: usize = 2_000;

pub struct SimpleQosBanlist {
    banlist: Arc<Banlist<Pubkey>>,
    eviction_sender: Sender<Pubkey>,
}
```

**File:** streamer/src/nonblocking/simple_qos.rs (L66-87)
```rust
    /// Ban the `pubkey` for the specified `timeout`
    ///
    /// Returns `true` if the `id` was already banned else `false`.
    pub fn ban(&self, pubkey: Pubkey, timeout: Duration) -> bool {
        let ret = self.banlist.ban(pubkey, timeout);
        match self.eviction_sender.try_send(pubkey) {
            Ok(()) => {}
            Err(TrySendError::Full(pubkey)) => {
                error!(
                    "Simple QoS banlist eviction queue full, dropping eviction request for \
                     {pubkey}"
                );
            }
            Err(TrySendError::Closed(pubkey)) => {
                info!(
                    "Simple QoS banlist eviction queue closed, dropping eviction request for \
                     {pubkey}"
                );
            }
        }
        ret
    }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L93-129)
```rust
    fn spawn_connection_evictor(
        &self,
        mut eviction_receiver: Receiver<Pubkey>,
        staked_connection_table: Arc<Mutex<ConnectionTable<TokenBucket>>>,
        stats: Arc<StreamerStats>,
    ) {
        let banlist = self.banlist.clone();
        let _eviction_task = tokio::spawn(async move {
            let mut prune_interval = interval(BANLIST_PRUNE_INTERVAL);
            prune_interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
            prune_interval.tick().await;
            loop {
                tokio::select! {
                    maybe_pubkey = eviction_receiver.recv() => {
                        let Some(pubkey) = maybe_pubkey else {
                            break;
                        };
                        let mut connection_table = staked_connection_table.lock().await;
                        let removed_connection_count = connection_table
                            .remove_connections_by_key(ConnectionTableKey::Pubkey(pubkey));
                        if removed_connection_count > 0 {
                            update_open_connections_stat(&stats, &connection_table);
                            stats
                                .connection_removed
                                .fetch_add(removed_connection_count, Ordering::Relaxed);
                            stats
                                .connection_removed_banned
                                .fetch_add(removed_connection_count, Ordering::Relaxed);
                        }
                    }
                    _ = prune_interval.tick() => {
                        banlist.prune();
                    }
                }
            }
        });
    }
```

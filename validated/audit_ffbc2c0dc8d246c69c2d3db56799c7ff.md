### Title
Global unbounded growth of `PeerNetwork::pending_messages` via per-`(event_id, neighbor_key)` buffering quota with no aggregate cap - ([File: stackslib/src/net/unsolicited.rs])

### Summary
`can_buffer_data_message` enforces `max_buffered_nakamoto_blocks` (and `max_buffered_stackerdb_chunks`) only against the inbox belonging to a single `(event_id, neighbor_key)` key, and `buffer_sortition_data_message`/`buffer_stacks_data_message` insert into `self.pending_messages`/`self.pending_stacks_messages` keyed by that same per-connection tuple. There is no check anywhere that bounds the total number of entries, or total buffered `NakamotoBlocksData`, across all `(event_id, neighbor_key)` keys in the map.

### Finding Description
`can_buffer_data_message` at [1](#0-0)  only iterates over `msgs`, the message list belonging to one inbox (one `(event_id, neighbor_key)` key), and compares the per-type count to `self.connection_opts.max_buffered_nakamoto_blocks`/`max_buffered_stackerdb_chunks`. It never looks at `self.pending_messages` as a whole.

`buffer_sortition_data_message` and `buffer_stacks_data_message` key their storage maps by `(event_id, neighbor_key)`: [2](#0-1)  and [3](#0-2) . Each new `(event_id, neighbor_key)` tuple starts its own inbox with its own quota of up to `max_buffered_nakamoto_blocks` unresolvable `NakamotoBlocksData` entries.

The call sites in `handle_unsolicited_sortition_messages` / `handle_unsolicited_stacks_messages` invoke these buffering functions once per message, per event: [4](#0-3)  and [5](#0-4) . Since the key includes `event_id` (which changes for every new TCP connection/conversation) and `neighbor_key` (which an attacker fully controls by generating a fresh keypair for each connection), an attacker who can authenticate many distinct short-lived connections (via `check_peer_authenticated`, requiring only a successful handshake with the attacker's own key — no privileged secret) can create an arbitrarily large number of distinct map keys, each contributing up to `max_buffered_nakamoto_blocks` `NakamotoBlocksData` entries referencing a consensus hash whose sortition never resolves (so the entries are never drained by re-processing on view update, since `is_nakamoto_block_bufferable` keeps returning "buffer" for an unknown sortition — [6](#0-5) ).

The equality the question poses — "total buffered `NakamotoBlocksData` entries across all `event_id`s is globally bounded" — does not hold in the code as written: the only cap present is per-key (`can_buffer_data_message`), and nothing in `PeerNetwork` bounds the number of distinct keys or the aggregate entry count of `pending_messages`/`pending_stacks_messages`.

### Impact Explanation
Each additional authenticated connection/neighbor-key pair used by the attacker adds a fresh quota window of up to `max_buffered_nakamoto_blocks` buffered `NakamotoBlocksData` messages (each potentially containing multiple `NakamotoBlock`s, subject to `MAX_MESSAGE_LEN`/wire size limits per message, but not aggregate memory limits) that persist in `self.pending_messages` for as long as the referenced sortition never resolves. This is a memory-growth DoS against the victim node's P2P layer, driven purely by the number of distinct authenticated `(event_id, neighbor_key)` pairs the attacker can establish over time, not by the per-connection quota. This matches the "Critical - remote crash/unauthenticated DoS from few messages" category in spirit (unauthenticated *memory-exhaustion* DoS), though it is worth noting the growth rate is gated by how fast an attacker can establish new authenticated connections/keys rather than by a single message.

### Likelihood Explanation
Preconditions: attacker needs to be able to complete the P2P handshake as an authenticated peer (achievable by any remote party with their own keypair — no privileged secret required), open multiple connections/rotate neighbor keys, and reference a `consensus_hash` for a sortition the node doesn't have (e.g., far in the future or from a divergent burnchain fork), so blocks are perpetually "bufferable" and never expire from `pending_messages`. The main constraint on exploitation speed is the node's own connection-count limits (`num_clients`/`max_neighbors`) and rate of accepting new connections — I was unable to fully verify in the available time whether `pending_messages`/`pending_stacks_messages` entries are purged when a peer's connection/event is torn down (the cleanup logic, if any, lives in `stackslib/src/net/p2p.rs`, e.g. around `deregister_peer`, which I located but did not fully inspect). If entries are *not* purged on disconnect, the attacker can churn connections indefinitely to grow the map without bound over the life of the node, which would make this a straightforward Critical-severity unauthenticated memory-exhaustion DoS. If entries *are* purged on disconnect, the exposure is instead bounded by the number of concurrently authenticated connections (still lacking a global cap on total buffered entries, but rate-limited by connection-slot exhaustion, which per the question's rules is out of scope as "connection-slot exhaustion needing only traffic volume").

### Recommendation
Add a global cap in `PeerNetwork` (e.g., total entries across `pending_messages` and `pending_stacks_messages`, or a dedicated counter of buffered `NakamotoBlocksData`/`StackerDBPushChunk` across all keys) and enforce it in `can_buffer_data_message` in addition to the per-`(event_id, neighbor_key)` check. Additionally, verify that `pending_messages`/`pending_stacks_messages` entries are proactively removed when the corresponding `event_id` is deregistered/disconnected (in `PeerNetwork::deregister_peer` or equivalent in `stackslib/src/net/p2p.rs`), and consider expiring buffered entries referencing sortitions/tenures that remain unresolved past a bounded number of burnchain-view/tenure updates.

### Proof of Concept
Net test plan (to be run as a Devin/engineer task, using the `TestPeer` harness in `stackslib/src/net/`):
1. Spin up a victim `TestPeer` with `connection_opts.max_buffered_nakamoto_blocks` set to a small value (e.g., 2).
2. For `i` in `0..N` (N large, e.g., 50): open a new TCP connection to the victim, complete the P2P handshake with a freshly generated keypair (yielding a distinct `event_id`/`NeighborKey` pair each time), then send exactly `max_buffered_nakamoto_blocks` distinct `NakamotoBlocksData` messages, each referencing a `consensus_hash` not present in the victim's `SortitionDB` (so `is_nakamoto_block_bufferable` returns `true` indefinitely per [6](#0-5) ), then close the connection.
3. After all N connections, inspect `victim_peer.network.pending_messages.len()` and the sum of `inbox.messages.len()` across all entries.
4. Assert: total buffered `NakamotoBlocksData` count == `N * max_buffered_nakamoto_blocks`, growing linearly/unboundedly with `N`, rather than being capped at a fixed global constant — demonstrating the missing aggregate cap in `can_buffer_data_message` (`stackslib/src/net/unsolicited.rs:94-136`) and the storage call sites at lines 679-681 and 742-744.

### Citations

**File:** stackslib/src/net/unsolicited.rs (L94-136)
```rust
    pub(crate) fn can_buffer_data_message(
        &self,
        event_id: usize,
        msgs: &[StacksMessage],
        msg: &StacksMessage,
    ) -> bool {
        // check limits against connection opts, and if the limit is not met, then buffer up the
        // message.
        let mut nakamoto_blocks_data = 0;
        let mut stackerdb_chunks_data = 0;
        for stored_msg in msgs.iter() {
            match &stored_msg.payload {
                StacksMessageType::NakamotoBlocks(_) => {
                    nakamoto_blocks_data += 1;
                    if matches!(&msg.payload, StacksMessageType::NakamotoBlocks(..))
                        && nakamoto_blocks_data >= self.connection_opts.max_buffered_nakamoto_blocks
                    {
                        debug!(
                            "{:?}: Cannot buffer NakamotoBlocksData from event {} -- already have {} buffered",
                            &self.get_local_peer(), event_id, nakamoto_blocks_data
                        );
                        return false;
                    }
                }
                StacksMessageType::StackerDBPushChunk(_) => {
                    stackerdb_chunks_data += 1;
                    if matches!(&msg.payload, StacksMessageType::StackerDBPushChunk(..))
                        && stackerdb_chunks_data
                            >= self.connection_opts.max_buffered_stackerdb_chunks
                    {
                        debug!(
                            "{:?}: Cannot buffer StackerDBPushChunks from event {} -- already have {} buffered",
                            self.get_local_peer(), event_id, stackerdb_chunks_data
                        );
                        return false;
                    }
                }
                _ => {}
            }
        }

        true
    }
```

**File:** stackslib/src/net/unsolicited.rs (L143-168)
```rust
    pub(crate) fn buffer_sortition_data_message(
        &mut self,
        event_id: usize,
        neighbor_key: &NeighborKey,
        msg: StacksMessage,
    ) -> bool {
        let key = (event_id, neighbor_key.clone());
        let neighbor_addr = self.pending_peer_addr(event_id, neighbor_key);
        let Some(inbox) = self.pending_messages.get(&key) else {
            self.pending_messages.insert(
                key.clone(),
                PendingMessagesFrom::new(neighbor_addr, vec![msg]),
            );
            debug!(
                "{:?}: Event {} has 1 messages buffered",
                &self.get_local_peer(),
                event_id
            );
            return true;
        };

        // check limits against connection opts, and if the limit is not met, then buffer up the
        // message.
        if !self.can_buffer_data_message(event_id, &inbox.messages, &msg) {
            return false;
        }
```

**File:** stackslib/src/net/unsolicited.rs (L190-226)
```rust
    pub(crate) fn buffer_stacks_data_message(
        &mut self,
        event_id: usize,
        neighbor_key: &NeighborKey,
        msg: StacksMessage,
    ) -> bool {
        let key = (event_id, neighbor_key.clone());
        let neighbor_addr = self.pending_peer_addr(event_id, neighbor_key);
        let Some(inbox) = self.pending_stacks_messages.get(&key) else {
            // check limits against connection opts, and if the limit is not met, then buffer up the
            // message.
            if !self.can_buffer_data_message(event_id, &[], &msg) {
                return false;
            }
            debug!(
                "{:?}: buffer message from event {}: {:?}",
                self.get_local_peer(),
                event_id,
                &msg
            );
            self.pending_stacks_messages.insert(
                key.clone(),
                PendingMessagesFrom::new(neighbor_addr, vec![msg]),
            );
            debug!(
                "{:?}: Event {} has 1 messages buffered",
                &self.get_local_peer(),
                event_id
            );
            return true;
        };

        // check limits against connection opts, and if the limit is not met, then buffer up the
        // message.
        if !self.can_buffer_data_message(event_id, &inbox.messages, &msg) {
            return false;
        }
```

**File:** stackslib/src/net/unsolicited.rs (L359-392)
```rust
        let (sn_rc_opt, can_process) =
            self.find_nakamoto_block_reward_cycle(sortdb, nakamoto_block);
        let Some(sn_rc) = sn_rc_opt else {
            return false;
        };

        // Determine the epoch in which to apply the signer-signature ordering
        // rule. If the block's sortition hasn't been processed yet, fall back
        // to the burnchain tip, otherwise we'd refuse to buffer the very
        // blocks this method exists to buffer.
        let epoch_burn_height = match SortitionDB::get_block_snapshot_consensus(
            sortdb.conn(),
            &nakamoto_block.header.consensus_hash,
        ) {
            Ok(Some(block_sn)) => block_sn.block_height,
            _ => {
                debug!(
                    "{:?}: no sortition yet for block {} consensus hash {}; use burnchain tip epoch",
                    self.get_local_peer(),
                    &nakamoto_block.header.block_hash(),
                    &nakamoto_block.header.consensus_hash,
                );
                self.burnchain_tip.block_height
            }
        };
        let epoch_id = self.get_epoch_at_burn_height(epoch_burn_height).epoch_id;

        if !self.check_nakamoto_block_signer_signature(sn_rc, epoch_id, nakamoto_block) {
            return false;
        }

        // the block is well-formed, but we'd buffer if we can't process it yet
        !can_process
    }
```

**File:** stackslib/src/net/unsolicited.rs (L646-682)
```rust
        unsolicited.retain(|(event_id, neighbor_key), inbox| {
            debug!("{:?}: Process {} unsolicited sortition-bound messages from {:?}", &self.get_local_peer(), inbox.messages.len(), neighbor_key; "buffer" => %buffer);
            inbox.messages.retain(|message| {
                if buffer
                    && !self.can_buffer_data_message(
                        *event_id,
                        self.pending_messages
                            .get(&(*event_id, neighbor_key.clone()))
                            .map(|inbox| inbox.messages.as_slice())
                            .unwrap_or(&[]),
                        message,
                    )
                {
                    // unable to store this due to quota being exceeded
                    debug!("{:?}: drop message to quota being exceeded: {:?}", self.get_local_peer(), &message.payload.get_message_description());
                    return false;
                }

                if !buffer {
                    debug!(
                        "{:?}: Re-try handling buffered sortition-bound message {} from {:?}",
                        self.get_local_peer(),
                        &message.payload.get_message_description(),
                        &neighbor_key
                    );
                }
                let (to_buffer, relay) = self.handle_unsolicited_sortition_message(
                    sortdb,
                    chainstate,
                    *event_id,
                    &message.payload,
                    buffer,
                );
                if buffer && to_buffer {
                    self.buffer_sortition_data_message(*event_id, neighbor_key, message.clone());
                    return false;
                }
```

**File:** stackslib/src/net/unsolicited.rs (L720-745)
```rust
        unsolicited.retain(|(event_id, neighbor_key), inbox| {
            if inbox.messages.is_empty() {
                // no messages for this node
                return false;
            }
            debug!("{:?}: Process {} unsolicited tenure-bound messages from {:?}", &self.get_local_peer(), inbox.messages.len(), &neighbor_key; "buffer" => %buffer);
            inbox.messages.retain(|message| {
                if !buffer {
                    debug!(
                        "{:?}: Re-try handling buffered tenure-bound message {} from {:?}",
                        &self.get_local_peer(),
                        &message.payload.get_message_description(),
                        neighbor_key
                    );
                }
                let (to_buffer, relay) = self.handle_unsolicited_stacks_message(
                    chainstate,
                    *event_id,
                    &message.preamble,
                    &message.payload,
                    buffer,
                );
                if buffer && to_buffer {
                    self.buffer_stacks_data_message(*event_id, neighbor_key, message.clone());
                    return false;
                }
```

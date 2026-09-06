### Title
Unbounded memory growth in `pending_messages` via per-(event_id, neighbor_key) buffering quota reset on reconnect - (File: `stackslib/src/net/unsolicited.rs`)

### Summary
`can_buffer_data_message` and `buffer_sortition_data_message`/`buffer_stacks_data_message` scope the buffering quota to the pair `(event_id, NeighborKey)` rather than to the peer's persistent identity. Because `event_id` is a fresh, monotonically-assigned value for every new TCP connection/`ConversationP2P`, a remote peer that completes a cheap Handshake, sends up to `max_buffered_nakamoto_blocks`/`max_buffered_stackerdb_chunks` unsolicited `NakamotoBlocksData`/`StackerDBPushChunk` messages, disconnects, and reconnects gets an entirely fresh quota bucket each time, letting `self.pending_messages` grow without bound until the next burnchain-tip change flushes it.

### Finding Description
The buffering quota logic lives in `can_buffer_data_message` [1](#0-0) , which only inspects the messages already stored under the *same* `(event_id, neighbor_key)` bucket that is passed in. The buffering functions key `self.pending_messages`/`self.pending_stacks_messages` by `(event_id, neighbor_key.clone())`: [2](#0-1) 
and the top-level dispatcher `handle_unsolicited_sortition_messages` re-derives the quota check from the *same* key before calling `buffer_sortition_data_message`: [3](#0-2) 

`event_id` is assigned per accepted socket/conversation, not per authenticated public key. A remote party can:
1. Open a TCP connection and complete a Handshake (cheap, requires no secret, no privileged role — any remote peer can do this).
2. Send `max_buffered_nakamoto_blocks` (or `max_buffered_stackerdb_chunks`) unsolicited data messages referencing a not-yet-processed sortition/tenure, filling the quota for that `event_id`.
3. Disconnect and reconnect (or open another connection), obtaining a new `event_id`. The quota check in `can_buffer_data_message` sees an empty bucket for the new `(event_id, neighbor_key)` key and allows buffering up to the cap again.
4. Repeat indefinitely (bounded only by how fast connections can be cycled, not by the node's per-neighbor buffering intent).

`self.pending_messages` is only cleared wholesale when the burnchain tip actually changes (`mem::replace(&mut self.pending_messages, HashMap::new())` in `p2p.rs`), which happens on the order of the block/sortition interval — not on a per-connection or per-identity basis. There is no eviction of stale `pending_messages` entries when a conversation/event is torn down, and no code that aggregates or caps buffered messages by the peer's persistent identity (e.g., public key hash). This breaks the intended invariant that a single peer identity has a bounded buffering budget: the real cap is per `event_id`, which the attacker fully controls by reconnecting.

### Impact Explanation
Between burnchain-tip changes, an attacker using a small, bounded number of reconnects (not raw packet/byte volume) can cause `pending_messages` to accumulate messages proportional to `(number of reconnects) × max_buffered_nakamoto_blocks/stackerdb_chunks`, each message up to the wire size limit permitted for `NakamotoBlocksData`/`StackerDBPushChunk`. This is unauthenticated-cost, authenticated-only-once-per-connection memory growth on the node process, matching a Critical DoS characterization (memory exhaustion from a bounded number of reconnect/message events rather than bandwidth flooding). The affected party is any Stacks node accepting P2P inbound peers.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to be able to open a P2P connection and complete a standard Handshake (no secret, no StackerDB slot ownership, no privileged role required). The node must simply be between sortition/tenure-tip updates, which is the normal steady state most of the time. Reconnect cost is low (one handshake round trip). The only mitigating factor not fully verified here is whether an outer connection-rate/IP-based throttle in `p2p.rs` (`num_clients`/socket limits) meaningfully restricts reconnect frequency — I could not confirm within the available context whether such a limit fully bounds the accumulation, or whether stale `pending_messages` entries for closed event_ids are otherwise reaped before the next tip change.

### Recommendation
Key the buffering quota (and ideally the `pending_messages`/`pending_stacks_messages` maps) by a stable peer identity (e.g., the neighbor's public key hash from the Handshake) instead of `(event_id, NeighborKey)`, so that quota state survives across reconnects. Additionally, evict/prune `pending_messages` entries tied to an `event_id` when the corresponding conversation is deregistered/disconnected, and enforce a global cap on total buffered messages/bytes across all keys.

### Proof of Concept
In `stackslib/src/net/tests/relay/nakamoto.rs`, extend `test_buffer_data_message` (which already exercises `buffer_sortition_data_message`/`can_buffer_data_message` directly, see [4](#0-3) ) to:
1. Fill the quota for `event_id = 0` with `peer_nk` up to `max_buffered_nakamoto_blocks`, and assert the next `buffer_sortition_data_message(0, &peer_nk, ...)` returns `false` (existing behavior).
2. Call `buffer_sortition_data_message(1, &peer_nk, ...)` (same `neighbor_key`, new `event_id`) `max_buffered_nakamoto_blocks` times and assert it returns `true` each time — demonstrating the quota reset.
3. Repeat for `event_id = 2, 3, ... N`, and assert that `peer.network.pending_messages.iter().filter(|((_, nk), _)| nk == &peer_nk).fold(0, |acc, (_, inbox)| acc + inbox.messages.len())` grows linearly with `N` well past `max_buffered_nakamoto_blocks`, proving there is no cap on cumulative buffered messages per neighbor identity across `event_id` churn.

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

**File:** stackslib/src/net/unsolicited.rs (L646-681)
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
```

**File:** stackslib/src/net/tests/relay/nakamoto.rs (L408-425)
```rust
    for _ in 0..peer.network.connection_opts.max_buffered_nakamoto_blocks {
        assert!(peer
            .network
            .buffer_sortition_data_message(0, &peer_nk, nakamoto_block.clone()));
    }
    assert!(!peer
        .network
        .buffer_sortition_data_message(0, &peer_nk, nakamoto_block));

    for _ in 0..peer.network.connection_opts.max_buffered_stackerdb_chunks {
        assert!(peer
            .network
            .buffer_stacks_data_message(0, &peer_nk, stackerdb_chunk.clone()));
    }
    assert!(!peer
        .network
        .buffer_stacks_data_message(0, &peer_nk, stackerdb_chunk));
}
```

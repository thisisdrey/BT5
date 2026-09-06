## Title
Unsolicited-message future buffers (`pending_messages` / `pending_stacks_messages`) are bounded only by per-key message count, not byte size, enabling memory exhaustion via oversized `NakamotoBlocksData`/`StackerDBPushChunk` messages - (File: `stackslib/src/net/unsolicited.rs`)

## Summary
`PeerNetwork::can_buffer_data_message` gates buffering of unsolicited `NakamotoBlocks` and `StackerDBPushChunk` messages purely by counting the number of already-buffered messages of each type against `max_buffered_nakamoto_blocks` / `max_buffered_stackerdb_chunks`. It never accounts for the serialized byte size of the buffered payloads, so an attacker who can establish an authenticated P2P conversation can fill each buffer slot with a maximum-size message, multiplying memory use by the wire size limit instead of a fixed byte budget — the same bug class as the Besu `FutureMessageBuffer` advisory (count-based caps instead of byte-based caps).

## Finding Description
`can_buffer_data_message` iterates the already-buffered messages for a given `(event_id, neighbor_key)` and only compares counts to the configured limits: [1](#0-0) 

`buffer_sortition_data_message` and `buffer_stacks_data_message` push the entire `StacksMessage` (including its full payload — a `NakamotoBlocksData` block or a `StackerDBPushChunkData` chunk) into `pending_messages` / `pending_stacks_messages` once this count check passes: [2](#0-1) [3](#0-2) 

These buffers are keyed per `(event_id, neighbor_key)` in a `HashMap`, and are cleared/retried only on burnchain/stacks-tip transitions: [4](#0-3) 

The connection options only expose *count* limits (`max_buffered_nakamoto_blocks`, `max_buffered_stackerdb_chunks`), with no byte-size budget field alongside them: [5](#0-4) 

Because each buffered message can be as large as the wire format allows a single P2P message to be (a `StacksMessage` payload, up to the protocol's message-size limit), the total memory consumed per neighbor key is `count_limit × max_message_size`, not a fixed byte budget. Each distinct `(event_id, neighbor_key)` gets its *own* independent quota (the count check only inspects `msgs` for that specific key), so multiple simultaneous/rotating authenticated conversations from the same or different peers each get a fresh per-key allowance, and the aggregate buffered memory across all live event/neighbor keys is unbounded by any single configuration knob — this exactly mirrors the Besu advisory's root cause: "FutureMessageBuffer... capped retained messages by count... not by bytes."

This differs from the StackerDB storage path (`try_replace_chunk`/`validate_received_chunk`), which does correctly enforce a byte-size cap (`config.chunk_size`) before persisting a chunk to disk: [6](#0-5) [7](#0-6) 

However, the *in-memory, pre-storage buffering path* in `unsolicited.rs` for both `StackerDBPushChunk` and `NakamotoBlocks` messages has no equivalent byte-size gate — it only checks per-type message counts, so a peer can enqueue up to `max_buffered_stackerdb_chunks` chunks each carrying up to `STACKERDB_MAX_CHUNK_SIZE` (16 MB) of data, or up to `max_buffered_nakamoto_blocks` `NakamotoBlocksData` messages each carrying a maximum-size block, before any subsequent size/signature/tip validation is applied on retry.

## Impact Explanation
An authenticated peer (handshake required, but no validator/signer privilege needed) can repeatedly send unsolicited `NakamotoBlocksData` or `StackerDBPushChunk` messages carrying maximum-size payloads whenever the buffering path is reachable (i.e., whenever the local burnchain/stacks tip doesn't yet match, which is a routine, attacker-triggerable condition via normal tip lag). Each such message is retained in `pending_messages`/`pending_stacks_messages` bounded only by count, not bytes, so the buffered memory footprint per key scales with `count_limit × max_payload_size` rather than a fixed budget. This is a bounded-but-large memory-amplification vector consistent with a Medium-severity Besu-style analog; it does not by itself corrupt state, forge data, or bypass authentication, but it can meaningfully inflate node memory use with comparatively few authenticated messages, mapping to the "bounded compute/memory DoS" category described in scope rather than pure volumetric flooding.

## Likelihood Explanation
Reaching the vulnerable code path only requires an already-authenticated P2P conversation (handshake completed) sending unsolicited `NakamotoBlocks` or `StackerDBPushChunk` payloads while the local node's burnchain/stacks tip view lags — a state that occurs naturally and can be encouraged by the attacker's own behavior (e.g., sending immediately after a tip advances, or timing sends around expected transitions). No validator/signer key or elevated privilege is required, only a standard peer connection, making this readily reachable by any remote peer capable of completing a handshake.

## Recommendation
Extend `can_buffer_data_message` (and the surrounding `buffer_sortition_data_message` / `buffer_stacks_data_message` functions) to track and enforce a total byte-size budget per buffer (and ideally a global budget across all `(event_id, neighbor_key)` entries), similar to the fix applied in the Besu advisory: compute a message's serialized size and reject/evict when the aggregate buffered bytes would exceed a configured cap, rather than relying solely on per-type message counts.

## Proof of Concept
1. Establish an authenticated P2P connection to a target node (standard handshake).
2. Wait for (or induce) a state where the node's `burnchain_tip`/`stacks_tip` is momentarily behind the sender's view, so `handle_unsolicited_sortition_message`/`handle_unsolicited_stacks_message` returns `to_buffer = true` for `NakamotoBlocks`/`StackerDBPushChunk` payloads.
3. Send `max_buffered_nakamoto_blocks` (or `max_buffered_stackerdb_chunks`) unsolicited messages, each carrying the maximum allowed payload size (max-size `NakamotoBlocksData` block, or a `StackerDBPushChunkData` chunk up to `STACKERDB_MAX_CHUNK_SIZE` = 16 MB, per `libstackerdb/src/libstackerdb.rs:37`).
4. Observe that `can_buffer_data_message` accepts all of them (count-only check) — see the existing unit test `test_buffer_data_message` in `stackslib/src/net/tests/relay/nakamoto.rs:345-426`, which demonstrates the count-based acceptance/rejection boundary but never varies payload size, confirming the check is size-agnostic.
5. Repeat across multiple event IDs / neighbor keys to multiply the per-key buffered memory, since each `(event_id, neighbor_key)` maintains an independent quota with no global byte ceiling.

### Citations

**File:** stackslib/src/net/unsolicited.rs (L100-133)
```rust
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
```

**File:** stackslib/src/net/unsolicited.rs (L143-183)
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

        let debug_msg = format!(
            "{:?}: buffer message from event {} (buffered: {}): {:?}",
            self.get_local_peer(),
            event_id,
            inbox.messages.len() + 1,
            &msg
        );
        if let Some(inbox) = self.pending_messages.get_mut(&key) {
            // should always be reachable
            debug!("{}", &debug_msg);
            inbox.messages.push(msg);
        }
        true
    }
```

**File:** stackslib/src/net/unsolicited.rs (L190-241)
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

        let debug_msg = format!(
            "{:?}: buffer message from event {} (buffered: {}): {:?}",
            self.get_local_peer(),
            event_id,
            inbox.messages.len() + 1,
            &msg
        );
        if let Some(inbox) = self.pending_stacks_messages.get_mut(&key) {
            // should always be reachable
            debug!("{}", &debug_msg);
            inbox.messages.push(msg);
        }
        true
    }
```

**File:** stackslib/src/net/p2p.rs (L638-646)
```rust
    /// Pending messages (BlocksAvailable, MicroblocksAvailable, BlocksData, Microblocks,
    /// NakamotoBlocks) that we can't process yet, but might be able to process on a subsequent
    /// burnchain view update.
    pub pending_messages: PendingMessages,

    /// Pending messages (StackerDBPushChunk) that we can't process yet, but might be able
    /// to process on a subsequent Stacks view update
    pub pending_stacks_messages: PendingMessages,

```

**File:** stackslib/src/net/connection.rs (L414-418)
```rust
    pub antientropy_public: bool,
    /// maximum number of pushed Nakamoto Block messages we can buffer before processing
    pub max_buffered_nakamoto_blocks: u64,
    /// maximum number of pushed StackerDB chunk messages we can buffer before processing
    pub max_buffered_stackerdb_chunks: u64,
```

**File:** stackslib/src/net/stackerdb/mod.rs (L656-666)
```rust
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/db.rs (L406-409)
```rust
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }
```

### Title
Attacker-controlled StackerDB chunk buffer quota exhaustion causes silent, un-NACKed loss of legitimate signer chunks during view transitions - (File: stackslib/src/net/unsolicited.rs)

### Summary
`PeerNetwork::can_buffer_data_message` / `buffer_stacks_data_message` in `stackslib/src/net/unsolicited.rs` enforce a per-connection (`event_id`, `neighbor_key`) cap (`connection_opts.max_buffered_stackerdb_chunks`) on how many `StackerDBPushChunk` messages can be queued while our node's view is behind (`FutureView`). Any peer that reaches this cap on a given connection causes subsequent, otherwise-valid `StackerDBPushChunk` messages arriving on that same connection to be silently dropped, with no NACK and no retry — analogous to how the Opyn report's unaccounted pre-existing balance silently short-circuits the intended flash-deposit branch.

### Finding Description
`handle_unsolicited_StackerDBPushChunk` (`stackslib/src/net/stackerdb/mod.rs:742-856`) validates and, when our view is stale relative to the remote peer (`NackErrorCodes::FutureView`), returns `(true, false)` to indicate the chunk should be buffered for later reprocessing: [1](#0-0) 

The actual buffering is performed by `buffer_stacks_data_message`, which is gated by `can_buffer_data_message`: [2](#0-1) [3](#0-2) 

If the quota `max_buffered_stackerdb_chunks` is already met for that `(event_id, neighbor_key)` inbox, `buffer_stacks_data_message` returns `false`, and the caller in `handle_unsolicited_stacks_message` propagates `(false, false)` — meaning the message is neither buffered nor forwarded to the relayer, i.e., it is dropped outright: [4](#0-3) 

The doc comment for `buffer_sortition_data_message` explicitly states this behavior: "If there is no space for the message, then silently drop it." The same silent-drop semantics apply to `buffer_stacks_data_message`, and the `handle_unsolicited_sortition_messages` sweep confirms the intent: "unable to store this due to quota being exceeded" → drop. [5](#0-4) 

Because a single peer/connection owns this per-`(event_id, neighbor_key)` inbox, a peer that is a legitimate but limited-scope StackerDB participant (i.e., owns at least one slot and can produce validly-signed chunks) can flood its own connection with spam `StackerDBPushChunk` messages timed during a view mismatch, filling the buffer before a more important chunk relayed over that same connection (e.g., another signer's block pre-commit chunk being gossiped through this peer) arrives. The important chunk is then dropped with no NACK sent back and no mechanism for the sender to know it needs to retry. This mirrors the root cause of the changelog-noted, already-patched bug (`changelog.d/stackerdb-uploaded-chunk-event-loss.fixed`) for the HTTP-upload path, which explicitly warns this exact bug class ("stall consensus indefinitely when the lost chunk was a signer's block pre-commit") is present in this codebase; the P2P push/buffer path analyzed here uses the identical fail-silent design.

### Impact Explanation
Silently dropping legitimate `StackerDBPushChunk` messages (used by the Nakamoto signer set to gossip block-commit/vote data) with no error and no retry can delay or block propagation of consensus-critical signer messages during sortition/view transitions — precisely the scenario the project's own changelog entry flags as capable of stalling consensus. This is a bounded-message DoS against legitimate data delivery over a specific connection, achievable with only a handful of messages (bounded by `max_buffered_stackerdb_chunks`), not bulk traffic volume.

### Likelihood Explanation
Requires only being an authenticated P2P neighbor and owning a single slot in some StackerDB (a normal, unprivileged participant capability, not an admin/other-party key), plus timing spam during a `FutureView` window, which naturally recurs on every sortition/tenure change. No cryptographic breaks or privileged access are needed.

### Recommendation
Track buffering quotas per-slot/per-signer or globally with fairness (e.g., round-robin eviction / reserve capacity per distinct signer) instead of a single FIFO-fill cap per `(event_id, neighbor_key)`, and/or NACK the sender when a message cannot be buffered so senders/relayers can retry via another path instead of the message being silently and permanently lost for that view.

### Proof of Concept
1. Establish a P2P connection to a target node and become a recognized StackerDB slot owner (own key, single slot).
2. Trigger a view mismatch so `make_StackerDBChunksInv_or_Nack` returns `NackErrorCodes::FutureView` (e.g., advance local burnchain view ahead of target, per `stackslib/src/net/stackerdb/mod.rs:609-624`).
3. Send `max_buffered_stackerdb_chunks` validly-signed `StackerDBPushChunk` messages for your own slot over the same connection, as exercised by `test_handle_unsolicited_stackerdb_push_chunk_future_view_validation` / `test_buffer_data_message` in `stackslib/src/net/tests/relay/nakamoto.rs:345-425` and `1159-1305`.
4. Have (or wait for) a legitimate `StackerDBPushChunk` from another slot to be relayed to the target over the same connection during the same view window; observe it is dropped (`can_buffer_data_message` returns `false`), with no NACK sent and no record retained for retry.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L816-847)
```rust
            StacksMessageType::Nack(ref nack_data) => {
                if nack_data.error_code == NackErrorCodes::FutureView {
                    // Chunk corresponds to a known DB but the view of the sender is potentially in
                    // the future. We should buffer this in case it becomes storable, but don't store it yet.
                    // Also validate the chunk before buffering to prevent invalid data from being
                    // accepted (e.g. protect against big chunks with forged signatures).
                    let stackerdb_config = if let Some(config) =
                        self.get_stacker_db_configs().get(&chunk_data.contract_id)
                    {
                        config
                    } else {
                        return Ok((false, false));
                    };

                    let slot_versions =
                        match self.stackerdbs.get_slot_versions(&chunk_data.contract_id) {
                            Ok(versions) => versions,
                            Err(_) => {
                                return Ok((false, false));
                            }
                        };

                    if !self.validate_received_chunk(
                        &chunk_data.contract_id,
                        stackerdb_config,
                        &chunk_data.chunk_data,
                        &slot_versions,
                    )? {
                        return Ok((false, false));
                    }

                    return Ok((true, false));
```

**File:** stackslib/src/net/unsolicited.rs (L90-136)
```rust
    #[cfg_attr(test, mutants::skip)]
    /// Determine whether or not the system can buffer up this message, based on site-local
    /// configuration options.
    /// Return true if so, false if not
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

**File:** stackslib/src/net/unsolicited.rs (L185-220)
```rust
    #[cfg_attr(test, mutants::skip)]
    /// Buffer a message for re-processing once the stacks view updates.
    /// If there is no space for the message, then silently drop it.
    /// Returns true if buffered.
    /// Returns false if not.
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
```

**File:** stackslib/src/net/unsolicited.rs (L546-577)
```rust
    ) -> (bool, bool) {
        match payload {
            StacksMessageType::StackerDBPushChunk(ref data) => {
                // N.B. send back a reply if we're calling to buffer, since this would be the first
                // time we're seeing this message (instead of a subsequent time on follow-up
                // processing).
                let (can_buffer, can_store) = self
                    .handle_unsolicited_StackerDBPushChunk(
                        chainstate, event_id, preamble, data, buffer,
                    )
                    .unwrap_or_else(|e| {
                        info!(
                            "{:?}: failed to handle unsolicited {:?} when buffer = {}: {:?}",
                            self.get_local_peer(),
                            payload,
                            buffer,
                            &e
                        );
                        (false, false)
                    });
                if buffer && can_buffer && !can_store {
                    debug!(
                        "{:?}: Buffering {:?} to retry on next sortition",
                        self.get_local_peer(),
                        &payload
                    );
                }
                (can_buffer, can_store)
            }
            _ => (false, true),
        }
    }
```

**File:** stackslib/src/net/unsolicited.rs (L646-662)
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
```

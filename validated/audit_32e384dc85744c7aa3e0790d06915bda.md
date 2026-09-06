This confirms the hypothesis: `signers-{set}-{message_id}` contracts (e.g. `signers-0-1` for `BlockResponse`, `signers-0-2` for `StateMachineUpdate`, `signers-0-3` for `BlockPreCommit`) all pull the *same* signer-slot list from `.signers` via `stackerdb-get-signer-slots-page` [1](#0-0) [2](#0-1) , meaning slot ID → signer address mapping is identical across every message-id lane for a given signer set/reward cycle. The chunk signature, however, only commits to `(slot_id, slot_version, data_hash)` and never to the destination contract/lane [3](#0-2) , and `validate_received_chunk`/`try_replace_chunk` only check the signer address recovered for that `smart_contract_id`+`slot_id`, plus version freshness — nothing ties the signature to the specific lane it was issued for [4](#0-3) [5](#0-4) .

### Title
Cross-lane StackerDB chunk replay: a validly-signed chunk for one `signers-N-X` contract can be forged into another `signers-N-Y` contract — (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures authenticate only `(slot_id, slot_version, data_hash)`; they do not bind to the target `QualifiedContractIdentifier` (i.e., the `signers-{set}-{message_id}` lane). Because every message-id lane for a given signer set shares the same slot→signer assignment, any unprivileged network peer that observes a legitimately signed chunk on one lane can unsolicitedly push it into a different lane, where it will pass the "authenticated for this DB" check even though it was never intended for that DB.

### Finding Description
`SlotMetadata::auth_digest` hashes only `slot_id`, `slot_version`, and `data_hash` — the smart contract identifier is not part of the signed digest: [3](#0-2) 

`validate_received_chunk`, used both for chunk downloads and for unsolicited `StackerDBPushChunk` messages, resolves the expected signer purely from `(smart_contract_id, slot_id)` and then verifies the chunk's signature against that address — it never checks that the signature/slot metadata was produced *for* `smart_contract_id`: [4](#0-3) 

The same gap exists in the persistence layer's `try_replace_chunk`, which only checks slot ownership, staleness, and write-count — again with no contract binding in the signature check: [5](#0-4) 

Meanwhile, all `signers-{set}-{message_id}` lane contracts for a given signer set fetch the *identical* slot list from `.signers`: [1](#0-0) [2](#0-1) 

So a given signer's private key authorizes slot N identically in `signers-0-1` (`BlockResponse`), `signers-0-2` (`StateMachineUpdate`), and `signers-0-3` (`BlockPreCommit`) [6](#0-5) . A signer legitimately signs a `BlockResponse` chunk for the `BlockResponse` lane; the resulting `(slot_id, slot_version, sig, data)` tuple is equally "valid" if replayed as-is into the `StateMachineUpdate` or `BlockPreCommit` lane for the same slot, provided the version there hasn't already advanced past it. This is exactly the class of bug in the report: a signature produced for one context/purpose is silently accepted for a different, unintended context because the signed payload does not disambiguate the two.

This is reachable by any unprivileged P2P peer relaying `StackerDBPushChunk`/`StackerDBChunk` messages it captured on the wire from a legitimate broadcast — no signer key or admin role is needed, only observation of a previously broadcast, validly-signed chunk (StackerDB chunks are gossiped in the clear to all replicating nodes).

Note: `libsigner`'s `signer_message_payload_matches_lane` helper (in `libsigner/src/events.rs`) exists and *would* catch a type-prefix mismatch when a consuming signer/miner deserializes the `SignerMessage` and checks its type against the expected lane [7](#0-6) , but this check is application-level, exercised only in specific event-processing code paths I could not confirm are applied uniformly on all read paths (e.g., `stacks-signer/src/client/stackerdb.rs::get_messages` simply attempts codec deserialization and silently skips on failure without lane verification [8](#0-7) ). Even where the check exists, it is a best-effort filter at the consumer, not a network/authentication-layer guarantee — the underlying StackerDB write/replication layer itself (`stackslib/src/net/stackerdb/**`) has no contract-binding in the chunk signature, so the malformed/misplaced chunk is still accepted, stored, and propagated network-wide by nodes before any application-level filtering occurs.

### Impact Explanation
This allows unauthenticated write of forged/mismatched chunk data into a StackerDB replica other than the one it was signed for, and that data is then replicated network-wide via the normal StackerDB gossip/relay machinery (`handle_unsolicited_StackerDBPushChunk` explicitly forwards accepted chunks to the relayer for further propagation) [9](#0-8) . Depending on how a specific lane's consumer parses/relies on cross-lane exclusivity, this can pollute a `StateMachineUpdate` or `BlockPreCommit` lane with stale/foreign-context payloads under a legitimate signer's identity, potentially confusing downstream consensus/signer-state logic that assumes "if it's in lane X, the signer meant it as an X message." This matches "network-wide propagation of forged data" and, depending on downstream trust of lane-exclusivity, could contribute to steering signer state off the intended path.

### Likelihood Explanation
High feasibility for the write itself: StackerDB chunks are gossiped in plaintext to all replicating nodes, so any peer can capture a legitimately-signed chunk and no signer secret is required to replay it into a sibling lane. The main uncertainty (which I could not fully confirm from the indexed code) is how robust the various consumer-side lane checks (`signer_message_payload_matches_lane`, epoch/type matching in `monitor_signers.rs`) are across *every* place that reads StackerDB chunks — some consumers do implement type/lane matching, mitigating the ultimate impact, while the underlying network/storage layer accepts the cross-lane write unconditionally.

### Recommendation
Bind the signed digest (`SlotMetadata::auth_digest`) to the target `QualifiedContractIdentifier` (or equivalently the `MessageSlotID`/lane) in addition to `slot_id`, `slot_version`, and `data_hash`, so a chunk signature is only valid for the specific StackerDB contract it was created for. This closes the replay path without needing every consumer to independently re-validate lane/type consistency.

### Proof of Concept
1. Signer S has a private key that owns slot 0 in reward-cycle-0's signer set, which is shared by contracts `signers-0-1` (`BlockResponse`), `signers-0-2` (`StateMachineUpdate`), `signers-0-3` (`BlockPreCommit`).
2. S legitimately signs and broadcasts a `BlockResponse` payload as a `StackerDBChunkData{slot_id:0, slot_version:5, sig, data}` to `signers-0-1`.
3. Attacker A (any P2P peer) observes this chunk on the wire (gossip is unauthenticated read/observe).
4. A crafts a `StackerDBPushChunkData{contract_id: signers-0-2, chunk_data: <captured slot_id, slot_version, sig, data>}` and sends it unsolicited to a victim node.
5. `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` resolves `addr = get_slot_signer(signers-0-2, 0)`, which is the *same* `S` address (per `stackerdb-get-signer-slots`), and `slot_metadata.verify(&addr)` succeeds because the digest never referenced `signers-0-1` [4](#0-3) .
6. If `signers-0-2` slot 0's stored version is `< 5`, the chunk is accepted, stored, and re-gossiped as a legitimate `StateMachineUpdate`-lane write from S, even though S never intended or signed it for that lane.

### Citations

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** libstackerdb/src/libstackerdb.rs (L159-166)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L720-815)
```rust
    /// Handle unsolicited StackerDBPushChunk messages.
    /// Check to see that the message can be stored or buffered.
    ///
    /// Optionally, make a reply handle for a StackerDBChunksInv to be sent to the remote peer, in which
    /// the inventory vector is updated with this chunk's data.  Or, send a NACK if the chunk
    /// cannot be buffered or stored.
    ///
    /// Note that this can happen *during* a StackerDB sync's execution, so be very careful about
    /// modifying a state machine's contents!  The only modification possible here is to wakeup
    /// the state machine in case it's asleep (i.e. blocked on waiting for the next sync round).
    ///
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
    /// Returns (true, x) if we should buffer the message and try processing it again later.
    /// Returns (false, x) if we should *not* buffer this message, because it either *won't* be valid
    /// later, or if it can be stored right now.
    ///
    /// Returns (x, true) if we should forward the message to the relayer, so it can be processed.
    /// Returns (x, false) if we should *not* forward the message to the relayer, because it will
    /// *not* be processed.
    pub fn handle_unsolicited_StackerDBPushChunk(
        &mut self,
        chainstate: &mut StacksChainState,
        event_id: usize,
        preamble: &Preamble,
        chunk_data: &StackerDBPushChunkData,
        send_reply: bool,
    ) -> Result<(bool, bool), net_error> {
        let Some(naddr) = self
            .get_p2p_convo(event_id)
            .map(|convo| convo.to_neighbor_address())
        else {
            debug!(
                "Drop unsolicited StackerDBPushChunk: event ID {} is not connected",
                event_id
            );
            return Ok((false, false));
        };

        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }

                // patch inventory -- we'll accept this chunk
                let Some(slot_version) = data
                    .slot_versions
                    .get_mut(chunk_data.chunk_data.slot_id as usize)
                else {
                    error!(
                        "Chunk not accepted with slot_id {}, which is greater than our slot_versions array {} in {}",
                        chunk_data.chunk_data.slot_id,
                        data.slot_versions.len(),
                        chunk_data.contract_id
                    );
                    return Ok((false, false));
                };
                *slot_version = chunk_data.chunk_data.slot_version;

                // wake up the state machine -- force it to begin a new sync if it's asleep
                if let Some(stackerdb_syncs) = self.stacker_db_syncs.as_mut() {
                    if let Some(stackerdb_sync) = stackerdb_syncs.get_mut(&chunk_data.contract_id) {
                        stackerdb_sync.wakeup();
                    }
                }
            }
```

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
```rust
        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
```

**File:** libsigner/src/v0/messages.rs (L68-78)
```rust
define_u8_enum!(
/// Enum representing the stackerdb message identifier: this is
///  the contract index in the signers contracts (i.e., X in signers-0-X)
MessageSlotID {
    /// Block Response message from signers
    BlockResponse = 1,
    /// Signer State Machine Update
    StateMachineUpdate = 2,
    /// Block Pre-commit message from signers before they commit to a block response
    BlockPreCommit = 3
});
```

**File:** libsigner/src/events.rs (L734-749)
```rust
/// Whether a `SignerMessage` payload type is the one expected for the given contract message id.
///
/// `lane_message_id` is the trailing number in the `signers-X-{lane_message_id}` boot
/// contract. Each signer-message contract is dedicated to exactly one `SignerMessage`
/// variant, so the payload's type-prefix byte must map to the same numeric `MessageSlotID`.
///
/// Miner-only payloads (`BlockProposal`, `BlockPushed`, `MockProposal`, `MockBlock`) are not
/// written to a signer contract and never match.
fn signer_message_payload_matches_lane(
    payload_kind: SignerMessageTypePrefix,
    lane_message_id: u32,
) -> bool {
    payload_kind
        .msg_id()
        .is_some_and(|slot| slot.to_u32() == lane_message_id)
}
```

**File:** stacks-signer/src/client/stackerdb.rs (L255-281)
```rust
    /// Get all signer messages from stackerdb for the given slot IDs
    pub fn get_messages<T: SignerMessage<M>>(
        session: &mut StackerDBSession,
        slot_ids: &[u32],
    ) -> Result<Vec<T>, ClientError> {
        let mut messages = vec![];
        let send_request = || {
            session
                .get_latest_chunks(slot_ids)
                .map_err(backoff::Error::transient)
        };
        let chunk_ack = retry_with_exponential_backoff(send_request)?;
        for (i, chunk) in chunk_ack.iter().enumerate() {
            let Some(data) = chunk else {
                continue;
            };
            let Ok(message) = read_next::<T, _>(&mut &data[..]) else {
                if !data.is_empty() {
                    warn!("Failed to deserialize chunk data into a SignerMessage");
                    debug!("slot #{i}: Failed chunk ({}): {data:?}", &data.len(),);
                }
                continue;
            };
            messages.push(message);
        }
        Ok(messages)
    }
```

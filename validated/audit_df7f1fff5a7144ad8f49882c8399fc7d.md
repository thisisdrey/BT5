### Title
Cross-contract StackerDB chunk replay via signature that omits contract-id binding - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures authenticate only `(slot_id, slot_version, data_hash)` and never the target StackerDB contract. Because the same signer set (keyed by `reward_cycle % 2`) is shared across multiple independent `.signers-<0|1>-<message_id>` StackerDB contracts, a signer's slot assignment (address ↔ `slot_id`) is identical across all of these contracts. A legitimately signed chunk broadcast for one `.signers-*` contract can therefore be replayed unmodified, with a different `contract_id`, into another `.signers-*` contract at the same slot, and it will pass signature verification and be accepted/stored/re-gossiped there.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash` [1](#0-0) , and `SlotMetadata::verify()` recovers the public key from that digest and checks it against the expected owner address for the slot [2](#0-1) . The contract identifier is never part of what is signed.

Both the storage path (`StackerDBTx::try_replace_chunk`) and the network validation path (`StackerDBSync::validate_received_chunk`, used both for downloaded and for pushed/unsolicited chunks) look up the slot's owning address *for the given `smart_contract_id`* and then call `slot_desc.verify(&owner)` — but `smart_contract_id`/`contract_id` is supplied out-of-band by the caller/attacker and is not cryptographically bound to the signature at all: [3](#0-2) [4](#0-3) 

For unsolicited/gossiped chunks, `contract_id` comes straight from the attacker-controlled `StackerDBPushChunkData` message envelope, which is forwarded into `validate_received_chunk` and then into storage/broadcast without any binding to the signature: [5](#0-4) [6](#0-5) 

The signer-slot assignment for a given `signer_set` (i.e. `reward_cycle % 2`) is computed once from the boot `.signers` contract and is shared by every `.signers-<set>-<message_id>` StackerDB contract, since each such contract simply queries the same underlying `stackerdb-get-signer-slots-page` for its `signer_set`: [7](#0-6)  — and the node derives per-message-type contract IDs from the same reward-cycle-keyed page: [8](#0-7) 

Consequently, signer address `X` occupies `slot_id = k` in every `.signers-<set>-*` contract for the current reward cycle. Because the signature never commits to the contract identifier, a chunk `(slot_id=k, slot_version=v, data, sig)` that `X` legitimately signed and broadcast for contract `A` (e.g. `.signers-0-1`, BlockResponse) is a valid, verifiable chunk for slot `k` in contract `B` (e.g. `.signers-0-2`, StateMachineUpdate) as well, since `verify()` only checks `(slot_id, slot_version, data_hash)` against `X`'s public key hash — which is the owner of slot `k` in both contracts. An attacker (any p2p peer, no privileged role needed) can therefore relay/replay this data as a `StackerDBPushChunkData` message with `contract_id = B` instead of `A`. If the replayed version number is higher than `B`'s current slot version (trivially arranged by picking any A-chunk version higher than what B currently holds, which is common since counters are independent per contract), the store will accept it via `try_replace_chunk`, and the node will further re-broadcast it to other peers via `process_stacker_db_chunks`/`broadcast_message`, propagating forged/misattributed data network-wide.

This is a direct structural analog of the Keycloak CVE-2026-1529 bug class: a token (signature) is valid, but a security-relevant scoping field (organization ID / contract ID) that should have been cryptographically bound to it is not, allowing legitimate credentials to be "replayed" into an unintended context.

### Impact Explanation
This lets an unprivileged remote peer cause message-type confusion within the signer StackerDB ecosystem: e.g. inject a signer's legitimately-signed `BlockResponse` chunk bytes into the slot that is supposed to hold that signer's `StateMachineUpdate` (or vice versa), corrupting whatever consumer parses that slot (e.g. `StackerDBListener`/`GlobalStateEvaluator`, which deserializes chunks as `SignerMessageV0` per-contract-type) [9](#0-8) . Since data would fail to deserialize as the expected message type in most cases, the more concrete impact is authenticated-looking, unauthorized writes into a StackerDB the attacker does not control the content of, and forged-data propagation across the p2p network to all subscribers of the target StackerDB — matching the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" categories.

### Likelihood Explanation
Exploitation requires only observing (or being relayed) one legitimately signed chunk from any signer for any `.signers-*` contract in the current reward cycle (trivial, since these are actively gossiped every round) and then submitting/relaying it with a different `contract_id` via the standard `StackerDBPushChunk`/HTTP POST chunk paths. No secret key, admin role, or other party's credentials are needed — only a valid, previously observed signature is repurposed for a different but permitted-owner slot.

### Recommendation
Bind the target contract identifier (and ideally slot-owner reward-cycle/signer-set) into the signed digest in `SlotMetadata::auth_digest()` (or otherwise reject writes/pushes whose `contract_id` differs from the one under which the signature was originally produced/observed), so that a signature is only valid for the specific `(contract_id, slot_id, slot_version, data_hash)` tuple it was created for.

### Proof of Concept
1. Observe signer `X`'s legitimately broadcast `StackerDBPushChunkData{ contract_id: A (.signers-0-1), chunk_data: StackerDBChunkData{ slot_id: k, slot_version: v, sig, data } }` on the p2p network (any peer sees this via normal gossip).
2. Construct `StackerDBPushChunkData{ contract_id: B (.signers-0-2), chunk_data: <same fields unmodified> }`.
3. Send this as an unsolicited `StackerDBPushChunk` message to a victim node. `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` looks up slot `k`'s owner in contract `B` (also `X`, since both contracts share the reward-cycle signer-slot assignment), calls `slot_metadata.verify(&X)`, which succeeds because the signature never encoded `A` vs `B`.
4. If `v` exceeds `B`'s current version for slot `k`, `try_replace_chunk` accepts and stores the chunk under contract `B`, and the node relays it further via `process_stacker_db_chunks`/`broadcast_message`, propagating the forged association across the network.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L160-166)
```rust
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }
```

**File:** libstackerdb/src/libstackerdb.rs (L181-193)
```rust
    /// Verify that a given principal signed this chunk metadata.
    /// Note that the address version is ignored.
    pub fn verify(&self, principal: &StacksAddress) -> Result<bool, Error> {
        let sigh = self.auth_digest();
        let pubk = StacksPublicKey::recover_to_pubkey_without_validating_low_s(
            sigh.as_bytes(),
            &self.signature,
        )
        .map_err(|ve| Error::VerifyingError(ve.to_string()))?;

        let pubkh = Hash160::from_node_public_key(&pubk);
        Ok(pubkh == *principal.bytes())
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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-792)
```rust
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
```

**File:** stackslib/src/net/relay.rs (L2406-2453)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
                            if matches!(e, Error::StaleChunk { .. }) {
                                // This is a common and expected message, so log it as a debug and with a sep message
                                // to distinguish it from other message types.
                                debug!(
                                    "Dropping stale StackerDB chunk";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            } else {
                                warn!(
                                    "Failed to store chunk for StackerDB";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            }
                            continue;
                        } else {
                            log_stored_stackerdb_chunk(&sync_result.contract_id, &chunk, &origin);
                        }

                        if let Some(event_list) = all_events.get_mut(&sync_result.contract_id) {
                            event_list.push(chunk.clone());
                        } else {
                            all_events.insert(sync_result.contract_id.clone(), vec![chunk.clone()]);
                        }

                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
                    }
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1073)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
    }
```

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L250-266)
```rust
        for (chunk, slot_id) in chunks.into_iter().zip(slot_ids) {
            let Some(chunk) = chunk else {
                continue;
            };
            let Some(signer_entry) = &signer_entries.get(&slot_id) else {
                continue;
            };
            let Ok(signer_pubkey) = StacksPublicKey::from_slice(&signer_entry.signing_key) else {
                continue;
            };
            let address = StacksAddress::p2pkh(config.is_mainnet(), &signer_pubkey);
            if let Ok(SignerMessageV0::StateMachineUpdate(update)) =
                SignerMessageV0::consensus_deserialize(&mut chunk.as_slice())
            {
                global_state_evaluator.insert_update(address, update);
            }
        }
```

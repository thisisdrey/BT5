### Title
StackerDB chunk signatures lack contract/StackerDB domain separation, permitting cross-DB signed-chunk replay - (File: `libstackerdb/src/libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs`, `stackslib/src/net/stackerdb/mod.rs`, `stackslib/src/net/relay.rs`)

### Summary
`SlotMetadata::auth_digest` — the digest a StackerDB signer actually signs over — is computed only from `slot_id`, `slot_version`, and `data_hash`, with no binding to the `QualifiedContractIdentifier` (i.e., *which* StackerDB replica/contract) the chunk belongs to. Any code path that accepts a chunk (HTTP `POST /v2/stackerdb/.../chunks`, P2P `StackerDBPushChunk`, or StackerDB sync) verifies the signature only against `(slot_id, slot_version, data_hash)` and the slot's configured signer address for the *target* contract — never against the contract identifier itself. Consequently, a chunk validly signed for StackerDB contract A can be replayed byte-for-byte as a chunk for StackerDB contract B, and will be accepted as authentic, as long as the same signer address occupies the same `slot_id` in B (which is a realistic condition for the signer-message StackerDBs, which use the same reward-cycle signer set/ordering across sibling contracts).

### Finding Description
The signature digest is defined in `auth_digest`: [1](#0-0) 

It hashes `slot_id`, `slot_version`, and `data_hash` only — never the smart contract identifier that owns the slot. `SlotMetadata::verify` recovers the public key from this digest and compares its hash to the caller-supplied `principal`: [2](#0-1) 

All storage/validation call sites feed in the contract-specific expected signer, but never mix the contract identifier into what is actually verified:

- `StackerDBs::try_replace_chunk` looks up `slot_validation.signer` *for this contract* and calls `slot_desc.verify(&slot_validation.signer)`, but the digest being verified is contract-agnostic: [3](#0-2) 

- `PeerNetwork::validate_received_chunk` (used both for `StackerDBGetChunkData` sync results and unsolicited `StackerDBPushChunk`, including the `FutureView` buffering path) does the same: it fetches the *per-contract* configured signer address and verifies the same contract-agnostic digest: [4](#0-3) 

- The HTTP endpoint `RPCPostStackerDBChunkRequestHandler::try_handle_request` passes the caller-supplied `contract_identifier` straight into `try_replace_chunk`, and on success re-broadcasts the *same* chunk bytes as a `StackerDBPushChunk` for that contract, propagating it network-wide via `set_relay_message`: [5](#0-4) [6](#0-5) 

- `Relayer::process_stacker_db_chunks` similarly stores and then rebroadcasts chunks per-contract without any cross-contract signature binding: [7](#0-6) 

Because the signed digest carries no contract-specific salt/domain tag, an attacker who observes (via ordinary P2P gossip, or by querying the HTTP endpoint) a legitimately signed `StackerDBChunkData` for contract A can resubmit the identical `(slot_id, slot_version, sig, data)` tuple to contract B (either via the HTTP POST endpoint or as an unsolicited P2P `StackerDBPushChunk`). If contract B's slot table assigns the *same* signer address to the same `slot_id` (a configuration that is expected/likely for the sibling "message-type" signer StackerDB contracts sharing a reward-cycle signer set and ordering, e.g. `signers-0-xxx` / `signers-1-xxx`), the replayed chunk passes `SlotMetadata::verify` and is accepted by `try_replace_chunk` as though the signer had authorized it for contract B, then it is stored and rebroadcast to the whole network via `broadcast_message`/`set_relay_message`.

This breaks the intended equality "signature authorizes THIS StackerDB slot write" — the signer never intended their message to be valid under a different smart-contract/namespace, but the code treats "verified signature over (slot_id, slot_version, data_hash)" as equivalent to "verified signature over (this specific StackerDB contract's slot_id, slot_version, data_hash)".

### Impact Explanation
This is a remote, unauthenticated write-authorization bypass: an unprivileged network peer can inject forged/replayed data into a StackerDB contract that the original signer never authorized for that contract, and that data gets propagated network-wide (both via HTTP relay and P2P gossip broadcast), matching the "network-wide propagation of forged data" / "unauthenticated write to StackerDB" criteria. Depending on which StackerDB the collision occurs in (e.g. between the two Nakamoto signer message-type DBs for the same cycle, which by construction use the same ordered signer set), this could let an attacker cause the block/signature signer subsystem to accept or process an old/stale/cross-channel signed message as if freshly authorized for a different message channel, undermining data integrity guarantees StackerDB is relied on for.

### Likelihood Explanation
Exploitability requires: (1) observing at least one validly signed chunk on the network for some StackerDB contract (chunks are gossiped in the clear and are also queryable via the public HTTP endpoint, so this is trivial), and (2) a second StackerDB contract configured with the same signer address at the same `slot_id`. Condition (2) is not guaranteed for arbitrary contracts, but is architecturally likely for paired signer-message StackerDBs that share a reward-cycle's signer ordering. No secret key, admin role, or special access is needed — only passive observation plus a normal POST/P2P message, so this is squarely in the "remote, unprivileged" analog class required.

### Recommendation
Bind the signed digest to the specific StackerDB instance by including the `QualifiedContractIdentifier` (or an equivalent per-DB unique identifier) in `SlotMetadata::auth_digest`, so that a signature computed for one StackerDB contract cannot be replayed as valid for another. This requires updating `auth_digest`, `sign`, and `verify` in `libstackerdb/src/libstackerdb.rs`, and threading the contract identifier through all callers (`StackerDBs::try_replace_chunk`, `PeerNetwork::validate_received_chunk`, and their test helpers) so verification always incorporates the intended target contract.

### Proof of Concept
1. Let signer S own `slot_id = 0` in both StackerDB contract A (`signers-0-N`) and StackerDB contract B (`signers-1-N`), a realistic configuration since sibling signer-message StackerDBs for the same reward cycle share signer ordering.
2. S legitimately signs and pushes chunk `StackerDBChunkData { slot_id: 0, slot_version: 5, sig, data }` to contract A. This is observed on the wire (P2P gossip broadcast or via `GET .../chunks/0`).
3. Attacker (no keys required) resubmits the exact same `{slot_id: 0, slot_version: 5, sig, data}` tuple via `POST /v2/stackerdb/<B-address>/<B-contract>/chunks`, i.e. calling `RPCPostStackerDBChunkRequestHandler` for contract B, or by sending an unsolicited `StackerDBPushChunk` P2P message for contract B.
4. `StackerDBs::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`) looks up `slot_validation.signer` for contract B (which is S), calls `slot_desc.verify(&S)`, and because `auth_digest` never included the contract identifier, verification succeeds — the chunk is stored under contract B and then rebroadcast network-wide via `set_relay_message`/`broadcast_message`, even though S never signed anything intended for contract B.

### Citations

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-211)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
                    test_debug!(
                        "Failed to replace chunk {}.{} in {}: {:?}",
                        stackerdb_chunk.slot_id,
                        stackerdb_chunk.slot_version,
                        &contract_identifier,
                        &e
                    );
                    // Classify the rejection directly from the error. `StaleChunk` is the
                    // only retryable case (the normal version-bump handshake); everything
                    // else is terminal for an identical chunk. Anything unexpected (DB or
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
        }
```

**File:** stackslib/src/net/relay.rs (L2406-2452)
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
```

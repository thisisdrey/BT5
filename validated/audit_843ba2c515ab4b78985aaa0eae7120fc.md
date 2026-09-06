### Title
Cross-StackerDB Replay of Chunk Signatures Due to Missing Contract Binding in `SlotMetadata::auth_digest` — (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the message that a StackerDB signer signs over `slot_id`, `slot_version`, and `data_hash` only [1](#0-0) . The `smart_contract_id`/StackerDB instance identity is never part of the signed digest. Verification and storage of a chunk (`StackerDBTx::try_replace_chunk`) only check that the recovered signer matches the signer configured for that `(smart_contract, slot_id)` pair, not that the signature itself is bound to that contract [2](#0-1) . Because many StackerDB instances reuse the same signer address set across different contracts/rounds (e.g., per-reward-cycle `.signers-N-M` StackerDBs), a chunk validly signed for one StackerDB contract/slot can be replayed verbatim into another StackerDB contract where the same address is also a registered slot signer for the same `slot_id`, as long as the target slot's stored version is lower than the replayed `slot_version`.

### Finding Description
A StackerDB chunk write is authorized purely by recovering the public key from the signature over `(slot_id, slot_version, data_hash)` and comparing the resulting `Hash160` to the `signer` address recorded for that slot in the *target* contract's `slot_validation` table [3](#0-2) [4](#0-3) . No part of the authenticated message (nor `try_replace_chunk`'s logic) commits to which `smart_contract_id` the chunk is destined for.

This same unbounded signature is used both for the HTTP write path (`POST /v2/stackerdb/{contract}/chunks`, handled in `RPCPostStackerDBChunkRequestHandler`, which calls `tx.try_replace_chunk`) [5](#0-4)  and for the p2p gossip/sync path (`process_stacker_db_chunks` → `tx.try_replace_chunk`, followed by re-broadcast as `StackerDBPushChunk`) [6](#0-5) , and for unsolicited push validation (`validate_received_chunk`), which likewise only checks `slot_metadata.verify(&addr)` without any contract binding [7](#0-6) .

The equality being broken is: "a signature that authorizes writing chunk C into slot S of StackerDB X" is treated as equivalent to "a signature that authorizes writing chunk C into slot S of StackerDB Y," whenever the same signer address happens to own slot S in both X and Y. This is precisely the replay class described in the report: parameters that should be scoped to one context/round can be captured and reused in another context because no nonce/binding value ties the signature to that context.

### Impact Explanation
An unprivileged network peer that observes a legitimately-signed chunk (via the public read endpoint or via normal p2p gossip) can resubmit the exact same `(slot_id, slot_version, sig, data)` tuple against a *different* StackerDB contract instance where the same signer address is configured for that slot id — something that is common given that Stacks signer sets are frequently reused verbatim across successive reward-cycle StackerDB contracts. If accepted, this is an unauthenticated write into a StackerDB slot that the signer never intended to write in that DB/round, and the node further propagates it network-wide via `StackerDBPushChunk` re-broadcast [8](#0-7) , causing forged/stale content to be replicated and treated as the current, legitimate chunk for a different logical StackerDB. This matches "network-wide propagation of forged data" / "unauthenticated write to StackerDB."

### Likelihood Explanation
Exploitability only requires: (1) knowledge of one validly-signed chunk for some StackerDB contract (obtainable via any GET chunk request or by watching gossip), and (2) a second StackerDB contract that reuses the same signer address for the same `slot_id` with a currently-lower stored `slot_version`. Both conditions are realistic given the reward-cycle-based signer StackerDB naming/rotation scheme, and no special privileges, secret keys, or timing races are needed — the resubmission is a straightforward unauthenticated HTTP POST or crafted `StackerDBPushChunk` p2p message.

### Recommendation
Bind the signed digest to the specific StackerDB instance (and ideally the round/reward-cycle) by including the `smart_contract_id` (and any relevant `rc_consensus_hash`/reward-cycle identifier) inside `SlotMetadata::auth_digest()` before hashing, e.g.:
```rust
fn auth_digest(&self, contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(contract_id.to_string().as_bytes());
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
and thread the contract id through `sign`/`verify` call sites (`StackerDBChunkData::sign`/`verify`, `try_replace_chunk`, `validate_received_chunk`) so a signature can never be replayed across StackerDB instances.

### Proof of Concept
1. Signer `S` legitimately signs chunk `C` (`slot_id=3`, `slot_version=5`, `data`) for StackerDB contract `signers-1-1`, current version there is `4`, so the write succeeds via `POST /v2/stackerdb/signers-1-1/chunks`.
2. Attacker observes this chunk (via `GET /v2/stackerdb/signers-1-1/chunks/3` or p2p `StackerDBChunkInv`/push gossip).
3. Attacker checks that StackerDB contract `signers-2-1` also configures signer `S` for `slot_id=3`, and that its current stored version for slot 3 is `< 5` (true for a freshly rotated reward-cycle StackerDB).
4. Attacker sends `POST /v2/stackerdb/signers-2-1/chunks` with the identical `(slot_id=3, slot_version=5, sig, data)` payload.
5. `RPCPostStackerDBChunkRequestHandler` calls `tx.try_replace_chunk(&signers-2-1, &slot_desc, &chunk)`; `slot_desc.verify(&slot_validation.signer)` succeeds because the digest never included the contract id, and version `5 > 4` passes freshness, so the forged/replayed chunk is stored and re-broadcast to the network as a fresh `StackerDBPushChunk` [4](#0-3) [9](#0-8) .

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
```rust
    pub fn try_replace_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L271-283)
```rust
                let slot_metadata = if let Ok(Some(md)) =
                    tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id)
                {
                    md
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(
                            "Failed to load slot metadata after storing chunk".to_string(),
                        ),
                    ));
                };

```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-323)
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

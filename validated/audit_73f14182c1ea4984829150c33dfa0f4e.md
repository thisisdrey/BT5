### Title
StackerDB chunk signature does not bind to the target smart contract, enabling cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the message that a StackerDB writer signs over `(slot_id, slot_version, data_hash)` only. It never includes the `QualifiedContractIdentifier` of the StackerDB the chunk is destined for. Because a validly-signed chunk is cryptographically indistinguishable across different StackerDB contracts as long as `slot_id`/`slot_version`/`data_hash` line up, an unprivileged relayer can capture a legitimately signed chunk broadcast for one StackerDB contract and replay it into a different StackerDB contract in which the same signing address happens to also own that `slot_id`, causing the replica to accept and re-broadcast data that was never intended for that contract/context.

### Finding Description
The signature digest is built purely from slot-local fields, with no contract binding: [1](#0-0) 

`SlotMetadata::verify()` recovers the public key from the signature and only checks that the recovered `Hash160` matches the address that the *caller* looked up (via `get_slot_signer` for a *specific* contract + slot): [2](#0-1) 

The remote-facing validation path, `StackerDBSync::validate_received_chunk`, resolves the expected signer strictly per `(smart_contract_id, slot_id)` and then calls this same contract-agnostic `verify()`: [3](#0-2) 

If the recovered address happens to also be the assigned signer of the same `slot_id` in a *different* StackerDB contract (which is expected in practice — the Stacks signer set contracts `.signers-0-N` / `.signers-1-N` are re-derived every reward cycle and frequently retain the same address→slot_id ordering across cycles, per `boot/signers.clar`'s `stackerdb-signer-slots-0/1` and the `stacks-signer` client's `get_parsed_signer_slots`), then a chunk that was signed and legitimately stored for contract A also passes signature verification for contract B, because the digest never says "this chunk belongs to contract B."

Once `validate_received_chunk` returns true, the chunk is written via `StackerDBTx::try_replace_chunk` and then rebroadcast network-wide from `PeerNetwork::relay.rs::process_stacker_db_chunks`: [4](#0-3) 

This breaks the intended equality "signature authenticates (signer, THIS contract's slot state)" — instead it only authenticates "(signer, some slot state)", allowing data signed for one context to be stored/propagated as valid for a different context.

### Impact Explanation
This allows an unprivileged network participant (any relayer with the ability to receive one legitimately signed chunk and resend it against a different StackerDB contract) to inject stale/foreign but validly-signed data into a StackerDB replica, and have that data propagated network-wide via `broadcast_message`. This matches "network-wide propagation of forged/foreign data" and "non-canonical state served as canonical" — nodes end up storing and gossiping data under a contract context the original signer never authorized for that specific StackerDB.

The severity is bounded by the requirement that the replayed slot's version and data hash values happen to satisfy the target DB's freshness constraints (`data.slot_version >= expected_version`, `slot_version <= max_writes`, chunk size limits) — see: [5](#0-4) 

### Likelihood Explanation
Exploitability depends on the same signer address being assigned the same `slot_id` across two live StackerDB contracts, which is a realistic and even common occurrence given how Stacks signer-set StackerDBs are re-derived per reward cycle from an ordered signer list (`.signers-0-N`/`.signers-1-N`, `boot/signers.clar`). No secret key, admin role, or third-party impersonation is required — the attacker only needs to observe one broadcast chunk and replay it, which any p2p peer or StackerDB replica peer can do.

### Recommendation
Include the target `QualifiedContractIdentifier` (or an equivalent contract-scoped domain separator, e.g. contract's Clarity name/issuer bytes) inside `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB it was created for:
```rust
fn auth_digest(&self, smart_contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(smart_contract_id.serialize_to_vec());
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
This requires updating `sign`/`verify` call sites (`StackerDBChunkData::sign`/`verify`, `validate_received_chunk`, `try_replace_chunk`) to thread the contract identifier through, which is a breaking wire-format/consensus-adjacent change and needs careful rollout coordination.

### Proof of Concept
1. Signer `S` owns `slot_id = 3` in StackerDB contract `A` (`.signers-0-5`) and also owns `slot_id = 3` in StackerDB contract `B` (`.signers-0-6`), which is plausible since slot assignment is ordering-derived per cycle.
2. `S` legitimately signs and publishes chunk `C` (`slot_id=3, slot_version=1, data`) to contract `A`; this propagates via `process_stacker_db_chunks`/`broadcast_message`.
3. Attacker (any relayer) observes `StackerDBPushChunkData{contract_id: A, chunk_data: C}` on the wire.
4. Attacker resends the same `chunk_data: C` (unmodified signature) but with `contract_id: B` to a node.
5. On the receiving node, `validate_received_chunk` for contract `B` calls `get_slot_signer(B, 3)` → `S`, then `SlotMetadata::verify(&S)` succeeds because the digest never referenced `A` or `B`.
6. The chunk is accepted, stored under `B`'s slot 3, and rebroadcast network-wide as authentic data for contract `B`, even though `S` never signed anything intended for `B`.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L699-715)
```rust
        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/relay.rs (L2410-2452)
```rust
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

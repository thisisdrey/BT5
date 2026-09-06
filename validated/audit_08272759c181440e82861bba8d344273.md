### Title
Cross-StackerDB chunk replay due to missing contract binding in `SlotMetadata` signature - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest that is signed to authenticate a StackerDB chunk write — commits only to `slot_id`, `slot_version`, and `data_hash`. It never binds the signature to the specific StackerDB smart contract (`QualifiedContractIdentifier`) the chunk is being written to. Because multiple StackerDB replicas share the same signer-address-to-slot-id assignment (e.g. the per-reward-cycle `.signers-{0|1}-N` contracts, one per `MessageSlotID`, all populated from the same `signer_entries`/slot ordering), a chunk that was validly signed and broadcast for one contract can be replayed verbatim into a different contract at the same `slot_id`, and will pass all server-side checks, since none of them re-derive or check the contract identity against the signature.

### Finding Description
The authentication digest is: [1](#0-0) 

which hashes only `slot_id`, `slot_version`, and `data_hash`. The `verify()` routine recovers the public key from this digest and compares its hash against a caller-supplied `principal`: [2](#0-1) 

No parameter of `verify`/`auth_digest` identifies which StackerDB (contract) the chunk belongs to.

On the write path, `StackerDBTx::try_replace_chunk` looks up the expected signer for the *given* contract/slot via `get_slot_validation`, then calls `slot_desc.verify(&slot_validation.signer)`: [3](#0-2) 

This only checks that the recovered signer matches the expected signer for *this* contract's slot — it does not verify that the signature was produced *for this contract*. If the same signer address is assigned the same `slot_id` in another StackerDB contract (which is the normal configuration for the `.signers-{0|1}-N` family of contracts, all sharing the same slot ordering derived from the reward-set signer list, as shown by the shared `expected_stackerdb_slots` used across all `signers-{signer_set}-{message_id}` contracts): [4](#0-3) 

then a chunk signed by that key for contract A's slot X is *also* a valid signature for contract B's slot X, because `auth_digest()` produces the identical bytes in both cases (same `slot_id`, and if `slot_version`/`data_hash` also coincide or the attacker just reuses the same signed chunk with a higher un-used version in B).

The gossip/relay validation path (`validate_received_chunk`, used both for unsolicited push and sync-downloaded chunks) has the same gap — it resolves the expected signer strictly from the local `smart_contract_id` argument and calls `slot_metadata.verify(&addr)`, again without any contract binding in the digest: [5](#0-4) 

Any node that stores such a replayed chunk will then relay it further, since successful storage triggers a `StackerDBPushChunk` broadcast to peers: [6](#0-5) 

This breaks the intended equality "a signature over (slot_id, slot_version, data_hash) authenticates a write to *this specific* StackerDB" — an attacker who observes any legitimately-signed, publicly-gossiped chunk (chunks are unauthenticated-to-read and openly broadcast) can replay it into a different, unrelated StackerDB contract that happens to assign the same signer to the same slot_id, and have it accepted as if it were freshly authored for that contract, then have the local node itself re-propagate the forged-context chunk network-wide.

### Impact Explanation
This is a **remote, unprivileged, unauthenticated write** to StackerDB state: no possession of any private key is required — the attacker only needs to observe (or request) a chunk that was validly signed for contract A and resubmit it (via HTTP `POST /v2/stackerdb/.../chunks` or via P2P `StackerDBPushChunk`) targeting contract B, as long as the target slot's version in B is lower than the replayed chunk's version. Because storage triggers rebroadcast (`process_stacker_db_chunks` / `handle_unsolicited_StackerDBPushChunk`), the forged-context chunk is propagated network-wide as if authentically written for contract B, corrupting the derived application state that other nodes (signers, miners) build from that StackerDB's contents. This matches the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" criteria for Critical severity.

### Likelihood Explanation
Exploitability requires only: (1) a signer address configured with the identical `slot_id` across two or more live StackerDB contracts (the standard, in-repo pattern for the `.signers-{0|1}-{message_id}` set of contracts), and (2) obtaining any one legitimately signed, publicly broadcast chunk from one of those contracts, which is trivial since StackerDB chunk data and signatures are not secret and are broadcast to all peers replicating that DB. No cryptographic break or privileged access is needed, making this a high-likelihood, low-cost attack against any deployment using the multi-contract signer StackerDB layout that ships in this codebase.

### Recommendation
Include the target StackerDB contract identifier (`QualifiedContractIdentifier`) as part of the signed digest in `SlotMetadata::auth_digest()` (and thus in `StackerDBChunkData::sign`/`verify`), so a signature is only valid for the specific StackerDB it was produced for. This requires a wire-format/protocol version bump since it changes what bytes are signed, but is the correct fix; as a stop-gap, `try_replace_chunk` and `validate_received_chunk` could additionally re-verify that the presented signature was produced in a context that includes the contract id via an out-of-band binding, but this is fragile without changing the digest itself.

### Proof of Concept
1. Configure (or observe) a network where the same signer address `S` is registered at `slot_id = 3` in both StackerDB contract `A` (e.g. `.signers-0-1`) and contract `B` (e.g. `.signers-0-2`), as is standard for the per-reward-cycle signer message contracts.
2. Wait for (or induce) `S` to legitimately sign and push a chunk `StackerDBChunkData { slot_id: 3, slot_version: 5, sig, data }` into contract `A`. Capture it from the P2P broadcast or via `GET /v2/stackerdb/A/3`.
3. Submit the identical `(slot_id=3, slot_version, sig, data)` tuple to contract `B` via `POST /v2/stackerdb/B/chunks` (or replay it as an unsolicited `StackerDBPushChunk` targeting `B`), provided `B`'s current version at slot 3 is `< slot_version`.
4. `try_replace_chunk` on `B` calls `get_slot_validation(B, 3)` → signer `S`, then `slot_desc.verify(&S)`, which succeeds because `auth_digest()` never referenced contract `A` or `B`. The chunk is stored in `B` and then rebroadcast to the network by `process_stacker_db_chunks`, propagating data that was never actually authored/signed for contract `B`.

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

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-341)
```rust
    for signer_set in 0..2 {
        for message_id in 0..SIGNER_SLOTS_PER_USER {
            let contract_name =
                ContractName::try_from(format!("signers-{}-{}", &signer_set, &message_id)).unwrap();
            let signers = readonly_call(
                &mut peer,
                &latest_block_id,
                contract_name.clone(),
                ClarityName::from_literal("stackerdb-get-signer-slots"),
                vec![],
            )
            .expect_result_ok()
            .unwrap();

            debug!("Check .{}", contract_name);
            if signer_set == 0 {
                assert_eq!(signers.expect_list().unwrap(), vec![]);
            } else {
                assert_eq!(signers, expected_stackerdb_slots);
            }
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

**File:** stackslib/src/net/relay.rs (L2382-2452)
```rust
    /// Process newly-arrived chunks obtained from a peer stackerdb replica.
    /// Chunks that we store will be broadcast, since successful storage implies that they were new
    /// to us (and thus might be new to our neighbors)
    pub fn process_stacker_db_chunks(
        &mut self,
        rc_consensus_hash: &ConsensusHash,
        stackerdb_configs: &HashMap<QualifiedContractIdentifier, StackerDBConfig>,
        sync_results: Vec<StackerDBSyncResult>,
        event_observer: Option<&dyn StackerDBEventDispatcher>,
    ) -> Result<(), Error> {
        // sort stacker results by contract, so as to minimize the number of transactions.
        let mut sync_results_map: HashMap<QualifiedContractIdentifier, Vec<StackerDBSyncResult>> =
            HashMap::new();
        for sync_result in sync_results.into_iter() {
            if let Some(result_list) = sync_results_map.get_mut(&sync_result.contract_id) {
                result_list.push(sync_result);
            } else {
                sync_results_map.insert(sync_result.contract_id.clone(), vec![sync_result]);
            }
        }

        let mut all_events: HashMap<QualifiedContractIdentifier, Vec<StackerDBChunkData>> =
            HashMap::new();

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

### Title
Cross-StackerDB chunk-signature replay due to missing contract-ID binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the message that a signer's signature authenticates over as only `slot_id || slot_version || data_hash` [1](#0-0) . It never includes the StackerDB's `smart_contract_id`. Since the same signer address is frequently assigned to the same `slot_id` across multiple distinct StackerDB contracts (e.g. `signers-0-1`, `signers-0-2`, `signers-0-3` for a single signer set, which all derive their slot ordering from the same `.signers` contract call `stackerdb-get-signer-slots-page`) [2](#0-1) [3](#0-2) , a chunk signature that is valid (and publicly observable, since chunks are gossiped in cleartext via `StackerDBPushChunk`) for one contract is equally valid for any other contract where that signer occupies the same slot index. This is analogous to the CoW report: the signed payload omits a context field (there: `appData`; here: the target StackerDB/contract identity) that a verifier assumes is bound to the signature, letting an adversary redirect authenticated data into an unintended context.

### Finding Description
`try_replace_chunk`/`insert_chunk` validate a chunk purely against the per-(stackerdb, slot_id) state stored in the `chunks` table: signer address, current version, and max writes [4](#0-3) . The signature check, `SlotMetadata::verify`, recovers the public key from `auth_digest()` and compares its hash to the `signer` column for that `(stackerdb_id, slot_id)` [5](#0-4) . Nothing in this digest, nor in `validate_received_chunk` in `stackerdb/mod.rs`, ties the signature to the specific `smart_contract_id`/`QualifiedContractIdentifier` being written to [6](#0-5) .

Because signer-to-slot-index assignment is derived from the same underlying `.signers` slot list for every per-message-type contract in a signer set (`signers-{set}-1`, `signers-{set}-2`, `signers-{set}-3`, mapped by `MessageSlotID`) [7](#0-6) [8](#0-7) , a given signer's address typically lands on the identical `slot_id` in each of these sibling contracts. A network attacker who observes a validly-signed `StackerDBChunkData` broadcast for contract A (e.g. `BlockResponse`) can replay the same `(slot_id, slot_version, sig, data)` tuple as a push to contract B (e.g. `StateMachineUpdate` or `BlockPreCommit`) at the same slot, as long as B's current stored version for that slot is lower than the replayed version. `validate_received_chunk`/`try_replace_chunk` will accept it because the signature recovers to the correct signer address for that slot in B too — the contract-ID equality that the node implicitly assumes ("this signature authorizes exactly this StackerDB's slot") is never checked.

### Impact Explanation
This breaks the "authenticated vs stored" equality for StackerDB writes: the receiving node stores and re-gossips attacker-supplied (replayed) data as if the corresponding signer had authored it for that specific StackerDB/message type, even though the signer never authenticated data for that contract. Depending on how each message-type's consumer (e.g. `StackerDBListener`/`GlobalStateEvaluator`) parses payloads, this can inject bytes into the wrong message-type's slot that get deserialized as a different `SignerMessageV0` variant, corrupting per-signer state tracked by other nodes (e.g. `global_state_evaluator.insert_update`) [9](#0-8) . This is an unauthenticated/unauthorized write into StackerDB state that is then propagated network-wide via `broadcast_message`, matching the Critical class in the rules ("unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data").

### Likelihood Explanation
Likelihood is moderate-to-low: it requires (a) the attacker to observe/capture a legitimately-signed chunk for one contract (trivial, since StackerDB chunks are broadcast in cleartext), and (b) the same signer to occupy the identical slot index in a sibling contract with a lower current version there — a condition that commonly holds for the `signers-{set}-{1,2,3}` family within a reward cycle. It does not require the signer's private key, any special role, or high traffic volume.

### Recommendation
Bind the signature to the specific StackerDB contract by including `smart_contract_id` (or a hash/discriminant of it) inside `SlotMetadata::auth_digest()`, so a signature produced for one StackerDB cannot be replayed as valid for another. This mirrors the CoW recommendation of making the "context" field (there `appData`, here the target contract identity) part of what is cryptographically committed to, and rejecting chunks whose signature doesn't match the destination contract.

### Proof of Concept
1. Signer S is assigned slot 0 in both `signers-0-1` (BlockResponse) and `signers-0-2` (StateMachineUpdate) via the shared `.signers` slot list.
2. S signs and pushes `StackerDBChunkData{slot_id:0, slot_version:5, data: D}` to `signers-0-1`; this is broadcast in cleartext via `StackerDBPushChunk`.
3. Attacker captures this message and re-sends it (unmodified) as a `StackerDBPushChunk` targeting `signers-0-2`, with `contract_id = signers-0-2`, keeping the same `chunk_data` (slot_id 0, version 5, same signature, same data).
4. `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` looks up the signer for `(signers-0-2, slot 0)` (same address S), calls `slot_metadata.verify(&addr)`, which succeeds because `auth_digest()` never referenced which contract it belongs to [10](#0-9) .
5. If `signers-0-2`'s current version for slot 0 is `< 5`, `try_replace_chunk` stores D into `signers-0-2` and the node re-broadcasts it as legitimate `StateMachineUpdate` data from S [4](#0-3) [11](#0-10) , despite S never having signed anything for `signers-0-2`.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
        if slot_desc.slot_version > self.config.max_writes {
            return Err(net_error::TooManySlotWrites {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
                max_writes: self.config.max_writes,
            });
        }
        self.insert_chunk(smart_contract, slot_desc, chunk)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-697)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
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

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

**File:** libsigner/src/v0/messages.rs (L68-96)
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

define_u8_enum!(
/// Enum representing the slots used by the miner
MinerSlotID {
    /// Block proposal from the miner
    BlockProposal = 0,
    /// Block pushed from the miner
    BlockPushed = 1
});

impl MessageSlotIDTrait for MessageSlotID {
    fn stacker_db_contract(&self, mainnet: bool, reward_cycle: u64) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }
    fn all() -> &'static [Self] {
        MessageSlotID::ALL
    }
}
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

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L247-266)
```rust
        let chunks = initial_chunks_loader.load_chunks(config);

        let mut global_state_evaluator = GlobalStateEvaluator::new(HashMap::new(), address_weights);
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

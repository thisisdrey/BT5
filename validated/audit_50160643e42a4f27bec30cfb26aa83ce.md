This confirms the vulnerability. The `signers-{cycle}-{message_id}` contracts (e.g. `signers-0-1` for `BlockResponse`, `signers-0-2` for `StateMachineUpdate`, `signers-0-3` for `BlockPreCommit`) all derive their slot layout from the *same* underlying signer set/ordering for a given reward cycle, as shown by `signers_db_get_slots` iterating `for message_id in 0..SIGNER_SLOTS_PER_USER` and asserting each `signers-{set}-{message_id}` contract returns the identical `expected_stackerdb_slots` ordering [1](#0-0) . This means the same signer address owns the same `slot_id` across every message-type StackerDB for a given reward cycle [2](#0-1) .

### Title
Missing contract-identity binding in StackerDB slot signatures enables cross-replica chunk replay - (`libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest over only `slot_id`, `slot_version`, and `data_hash` — it never binds the target StackerDB's contract identifier [3](#0-2) . Because signers own the identical `slot_id` across every one of the sibling `.signers-{cycle}-{message_id}` StackerDB replicas for a reward cycle, a chunk validly signed and broadcast for one message-type contract (e.g. `BlockResponse`) is *also* a validly-signed chunk for a sibling contract (e.g. `StateMachineUpdate`, `BlockPreCommit`) at the same `slot_id`, as long as the version requirement is satisfied there.

### Finding Description
Chunk acceptance is gated purely on: signature verification against the *slot's* signer address (`get_slot_signer`/`get_slot_validation`), version monotonicity, and size/write-count limits — never on which contract the signature was actually produced for:

- `try_replace_chunk` in the storage layer verifies `slot_desc.verify(&slot_validation.signer)` and staleness, with no contract binding in the verified digest [4](#0-3) .
- `validate_received_chunk`, used both for chunks pushed unsolicited over p2p and for downloaded sync chunks, performs the same signature/version checks with no contract-specific data in what's verified [5](#0-4) .
- The signed digest itself, `auth_digest()`, only hashes `slot_id`, `slot_version`, and `data_hash` [3](#0-2) .

Since the `.signers-{cycle}-{msg_id}` contracts for `BlockResponse`, `StateMachineUpdate`, and `BlockPreCommit` are all populated from the same underlying reward-cycle signer ordering (as directly verified by the test asserting identical slot layouts across `message_id` values) [1](#0-0) , and each is looked up via `stacker_db_contract(mainnet, reward_cycle)` differing only by the numeric message-type suffix [6](#0-5) , a signer's own address occupies the same `slot_id` in each sibling replica. Any network observer who captures one legitimately-signed, broadcast `StackerDBChunkData` (e.g. a `BlockResponse` chunk) can resubmit that exact `(slot_id, slot_version, sig, data)` tuple as a `POST /v2/stackerdb/.../chunks` request, or as an unsolicited `StackerDBPushChunk`, against a *different* sibling contract (e.g. `StateMachineUpdate`) for the same reward cycle. If the target slot's stored version there is still below the replayed version, the write succeeds — the signature verifies (it never encoded which contract it was for), and the version/staleness check passes independently per-contract.

### Impact Explanation
This is an unauthenticated cross-context data-injection primitive: an attacker who is not a signer, and who did not craft or sign anything, can cause bytes explicitly signed by a legitimate signer for one purpose/contract to be accepted as-authored-and-current inside a different logically-independent StackerDB. Because `process_stacker_db_chunks` re-broadcasts every successfully-stored chunk to the whole network via `p2p.broadcast_message` [7](#0-6) , this forged (mis-contexted) association propagates network-wide and is delivered to every consumer (miners' `StackerDBListener`, signer state machines) as authentic data for that contract, even though the signer never intended to write it there. Consumers that deserialize the payload as the wrong `SignerMessage` variant will simply fail to parse and drop it in most cases, but the write still corrupts/overwrites the target slot's legitimate current chunk (destroying it, similar in spirit to the reported bug class of a stale/foreign write clobbering live state), and any case where the byte layout happens to parse as a plausible message of the target type would be a genuine forged-message injection.

### Likelihood Explanation
Exploitation requires no privileged role: the attacker only needs to observe one broadcast/pushed chunk (trivial, since these are gossiped over the wire and served over public HTTP `list_chunks` endpoints) and replay it to a sibling contract's endpoint, which is equally public. The main constraint is that the target slot's version there must still be behind the replayed version, which is often true in practice given that different message-type StackerDBs are written independently and at different cadences.

### Recommendation
Bind the target StackerDB contract identifier (and ideally the network/chain id) into `SlotMetadata::auth_digest()` so signatures are contract-scoped and cannot be replayed across sibling or unrelated StackerDB instances. This requires a coordinated protocol/version change since it alters the signed payload format across `libstackerdb`, `stackslib`'s StackerDB storage/validation layer, and `libsigner`/`stacks-signer` signing call sites.

### Proof of Concept
1. Reward cycle `N` has signer `S` at `slot_id = 3` in both `.signers-N-1` (`BlockResponse`) and `.signers-N-2` (`StateMachineUpdate`) — confirmed identical ordering per `signers_db_get_slots` [1](#0-0) .
2. Signer `S` legitimately signs and posts a `BlockResponse` chunk at `slot_id=3, slot_version=5` to `.signers-N-1`; it is accepted and gossiped network-wide via `process_stacker_db_chunks` [8](#0-7) .
3. An unprivileged attacker observes this chunk on the wire (or fetches it via the public chunk-listing RPC) and resubmits the identical `(slot_id=3, slot_version=5, sig, data)` to `.signers-N-2` (`StateMachineUpdate`) via `POST /v2/stackerdb/<StateMachineUpdate-contract>/chunks`.
4. `try_replace_chunk` verifies the signature against `.signers-N-2`'s slot-3 signer (also `S`, same address) — it succeeds because `auth_digest()` never included the contract identifier — and if `.signers-N-2`'s slot 3 was at a version `< 5`, the write is accepted and re-broadcast, overwriting the legitimate `StateMachineUpdate` chunk with `BlockResponse` bytes that were never signed for that contract.

### Citations

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

**File:** libsigner/src/v0/messages.rs (L90-96)
```rust
    fn stacker_db_contract(&self, mainnet: bool, reward_cycle: u64) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }
    fn all() -> &'static [Self] {
        MessageSlotID::ALL
    }
}
```

**File:** libsigner/src/v0/messages.rs (L126-140)
```rust
impl MessageSlotID {
    /// Return the StackerDB contract corresponding to messages of this type
    pub fn stacker_db_contract(
        &self,
        mainnet: bool,
        reward_cycle: u64,
    ) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }

    /// Return the u32 identifier for the message slot (used to index the contract that stores it)
    pub fn to_u32(self) -> u32 {
        self.to_u8().into()
    }
}
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

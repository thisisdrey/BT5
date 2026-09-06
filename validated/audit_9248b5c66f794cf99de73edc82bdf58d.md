### Title
StackerDB chunk signatures are not bound to the target smart contract, allowing cross-contract chunk replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures authenticate only `(slot_id, slot_version, data_hash)` and never the StackerDB smart-contract identity. Because multiple StackerDB contracts (e.g. all `signers-{set}-0..N` "lane" contracts for a given signer set/reward cycle) assign the *same* signer address to the *same* `slot_id`, a chunk a signer legitimately signed and posted to one contract can be replayed by any unprivileged, remote observer into a *different* contract that the signer never intended to write to, and the node will accept and store it as authentic.

### Finding Description
`SlotMetadata::auth_digest()` computes the signed hash as: [1](#0-0) 
i.e. `sha512/256(slot_id || slot_version || data_hash)`. The contract/database identity is never part of the digest. `verify()` only checks that the recovered public key hash equals a caller-supplied `principal`: [2](#0-1) 

That `principal` is looked up per-contract via `get_slot_signer(smart_contract_id, slot_id)`: [3](#0-2) 

and the actual write path (`try_replace_chunk`) only checks (a) chunk size, (b) `verify()` against that contract's configured signer for the slot, and (c) monotonic version — nothing ties the signature to the specific contract being written: [4](#0-3) 

The same fault exists in the peer-to-peer ingestion path, `PeerNetwork::validate_received_chunk`, which likewise only checks `get_slot_signer(smart_contract_id, slot_id)` + `slot_metadata.verify(&addr)` + version, never the contract identity itself: [5](#0-4) 

This authentication gap is directly exploitable because, for the boot `.signers` StackerDBs, the *same* signer-to-slot assignment is shared across every "lane" contract (`signers-{set}-0` … `signers-{set}-{message_id}`) of a signer set: they all delegate to the same underlying `stackerdb-get-signer-slots-page` state in `.signers`, so slot `N` is owned by the same address in every lane contract for that set: [6](#0-5) [7](#0-6) 

This is confirmed directly by the test asserting that every `signers-{set}-{message_id}` contract returns the identical slot list for a given signer set: [8](#0-7) 

Consequently, a `StackerDBChunkData` legitimately signed by signer A for slot 3, version 5 in `signers-1-1` (the `BlockResponse` lane) is *also* a validly-verifying chunk for slot 3 in `signers-1-6` (the `StateMachineUpdate` lane), because `verify()` recomputes the exact same digest and the exact same address owns slot 3 in both contracts. Nothing in `try_replace_chunk` or `validate_received_chunk` rejects this cross-contract reuse — the equality being broken is "signed-for-contract-A" vs "accepted-into-contract-B".

The only mitigating check is downstream, client-side filtering in `libsigner`'s `signer_message_payload_matches_lane`, which drops payloads whose type-prefix byte doesn't match the target lane's expected `MessageSlotID` when converting a `StackerDBChunksEvent` into a `SignerEvent`: [9](#0-8) 
That check happens only in the signer-runner's event consumer, not in the node's storage/replication layer (`stackslib/src/net/stackerdb/**`), which is squarely in scope per the rules. The node itself will accept, store, and gossip a chunk into a StackerDB slot for which the signature was never actually produced by the signer for that database — an unauthorized write to that StackerDB's state and unnecessary propagation of the forged-context chunk to the network. For lane contracts whose `message_id` is not recognized by `get_signers_db_signer_set_message_id`/`signer_message_payload_matches_lane` (unused message-id lanes), there is no filtering at all, so the cross-contract replay's forged write and its replication across the p2p network go entirely unmitigated at any layer of this repo.

### Impact Explanation
This breaks the intended per-database authentication guarantee of StackerDB: "a signature is proof that the signer authorized this bytes-for-this-database." An unprivileged remote party who merely observes chunks that are inherently public (served over HTTP `GET /v2/stackerdb/{contract}/chunks` and gossiped in the clear over p2p) can cause the node to accept and store a chunk into a *different* StackerDB contract/slot than the signer authorized, and the node will further gossip that forged-context chunk to its peers (`handle_unsolicited_StackerDBPushChunk` / chunk-inv exchange paths). This is an unauthenticated/unauthorized write to StackerDB state and propagation of forged (mis-contextualized) data across the network — matching the "Critical" impact bucket for unauthenticated/unauthorized StackerDB writes and network-wide propagation of forged data.

### Likelihood Explanation
High. No secrets or privileged roles are required — an attacker only needs to observe one legitimately-signed chunk (trivial, since chunks are served/broadcast in the clear) and re-POST it (or re-gossip it) against a different, but co-owned, StackerDB contract/slot with a satisfying version number. The `.signers` boot-contract topology (identical slot-to-signer mapping shared across all lane contracts of a signer set) makes a same-owner target trivially available for every signer, every reward cycle.

### Recommendation
Bind the smart-contract identity (and any other identifying context, such as the reward cycle) into the signed digest, e.g.:
```rust
hasher.update(smart_contract_id.serialize_to_vec()); // or a stable content hash of it
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
```
This requires updating `SlotMetadata::auth_digest`/`sign`/`verify` (and `StackerDBChunkData::sign`/`verify`/`recover_pk`) in `libstackerdb/src/libstackerdb.rs` to take the contract ID as an explicit signing input, and updating all signers/tests to include it. This is a wire/consensus-adjacent breaking change to the StackerDB chunk-signing format and would need coordinated versioning with existing signers.

### Proof of Concept
1. Let signer A own slot 3 in both `signers-1-1` (BlockResponse lane) and `signers-1-6` (StateMachineUpdate lane) for the current reward cycle (guaranteed by the shared `.signers` slot-list, as shown in `signers_db_get_slots`).
2. Observe (via `GET /v2/stackerdb/signers-1-1/chunks/3` or via p2p `StackerDBChunkInv`/`StackerDBGetChunk`) a chunk `{slot_id: 3, slot_version: 5, sig: S, data: D}` that A legitimately posted to `signers-1-1`.
3. As an unprivileged remote party, POST the identical tuple `{slot_id: 3, slot_version: 5, sig: S, data: D}` to `signers-1-6` via `POST /v2/stackerdb/signers-1-6/chunks` (handled by `RPCPostStackerDBChunkRequestHandler` → `try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs`), or inject it via the p2p `StackerDBPushChunk` unsolicited path (`PeerNetwork::validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs`), as long as `signers-1-6` slot 3's current version is `< 5`.
4. `slot_desc.verify(&slot_validation.signer)` succeeds because the digest `(3, 5, hash(D))` recovers to the same address A that owns slot 3 in `signers-1-6`; the version check passes; the chunk is written into `signers-1-6`'s slot 3 and subsequently gossiped to peers as canonical data for that database — even though signer A never signed anything intended for `signers-1-6`.

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-438)
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
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L530-543)
```rust
    /// Get the principal who signs a particular slot in a particular stacker DB.
    /// Returns Ok(Some(addr)) if this slot exists in the DB
    /// Returns Ok(None) if the slot does not exist
    /// Returns Err(..) if the DB doesn't exist of some other DB error happens
    pub fn get_slot_signer(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_id: u32,
    ) -> Result<Option<StacksAddress>, net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "SELECT signer FROM chunks WHERE stackerdb_id = ?1 AND slot_id = ?2";
        let args = params![stackerdb_id, slot_id];
        query_row(&self.conn, sql, args).map_err(|e| e.into())
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

**File:** libsigner/src/events.rs (L583-595)
```rust
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
```

### Title
Cross-StackerDB Chunk Signature Replay Due to Missing Contract Binding in Signed Digest - ([File: libstackerdb/src/libstackerdb.rs])

### Summary

### Finding Description
`SlotMetadata::auth_digest()` computes the digest that a StackerDB writer signs as `SHA512/256(slot_id || slot_version || data_hash)`: [1](#0-0) 

Notice the digest does **not** include the target smart-contract identifier (i.e., which StackerDB instance the chunk is destined for). Signature verification (`SlotMetadata::verify`) only checks that the recovered public-key hash matches the `signer` address recorded for that `(smart_contract, slot_id)` pair in the local `chunks`/`slot_validation` table: [2](#0-1) [3](#0-2) 

The signer→slot_id assignment for the Nakamoto signer StackerDBs comes from the shared `.signers` boot contract, which stores exactly two slot-assignment lists (`stackerdb-signer-slots-0` and `stackerdb-signer-slots-1`, keyed only by `reward_cycle % 2`), and every one of the several per-message-type contracts (`signers-{0|1}-{message_id}` for `message_id` in `0..SIGNER_SLOTS_PER_USER`) reads the *same* page via `stackerdb-get-signer-slots-page`: [4](#0-3) 

This is directly confirmed by the test showing identical slot lists returned by every `signers-{set}-{message_id}` contract for a given signer set: [5](#0-4) 

Consequence: For a fixed reward-cycle parity, `slot_id -> signer address` is **identical across all the per-message-type StackerDB contracts** (e.g. the BlockResponse contract and the Transactions/other message-type contracts). Since the signed digest binds only to `(slot_id, slot_version, data_hash)` — never to the contract/message-type — a chunk that a legitimate signer validly signed and broadcast for one contract (e.g. `signers-0-1`) will pass `SlotMetadata::verify()` unmodified when replayed into a *different* contract sharing the same slot table (e.g. `signers-0-2`), as long as the freshness/version and max-writes checks in `try_replace_chunk`/`validate_received_chunk` are satisfied: [6](#0-5) [7](#0-6) 

Because StackerDB chunks are relayed network-wide as public gossip (any node/peer can observe them via inventory/`StackerDBGetChunkData`/push-chunk flows), an unprivileged remote attacker only needs to observe a validly-signed chunk broadcast to one signer StackerDB contract, then resubmit that exact `(slot_id, slot_version, data, sig)` tuple via `POST /v2/stackerdb/.../chunks` (`RPCPostStackerDBChunkRequestHandler`) to a *different* message-type StackerDB contract that shares the same slot assignment. No private key is needed — this is a pure signature/message replay across databases: [8](#0-7) 

This is the direct analog of the reported "message not validated against the object/context it is bound to" flaw: the signature commits to slot id/version/data but not to *which* database (i.e., which message semantic) the signer intended it for.

### Impact Explanation
This breaks the "authenticated for this StackerDB/message type" equality: a message a signer legitimately produced for one purpose (e.g. a `BlockResponse`) can be forged into another message-type StackerDB (e.g. wherever `Transactions`/proposal responses are stored) as long as slot/version bookkeeping allows it, causing consumers of that StackerDB (miners, other signers, stackerdb-listener) to treat replayed/foreign data as a genuine, freshly-signed message for that context. This is unauthorized write / forged-data propagation into StackerDB state without possessing the signer's private key, matching the "unauthenticated/unauthorized write to state" and "network-wide propagation of forged data" impact classes.

### Likelihood Explanation
Exploitability depends on (a) an attacker observing a validly-signed chunk from a StackerDB sharing a slot table with the target contract (trivial, since these are public gossip / can be polled from disk via GET chunk endpoints), and (b) the version/write-count check in the target slot still permitting acceptance (`slot_version` must exceed the target's current stored version and be `<= max_writes`). Since the Nakamoto signer set intentionally shares one slot assignment across all `SIGNER_SLOTS_PER_USER` message-type contracts per reward-cycle parity, the precondition for replay is satisfied by design, not by coincidence, making this readily reachable for any of the always-co-existing per-message contracts of the same parity.

### Recommendation
Bind the signed digest to the destination StackerDB, e.g. include the `smart_contract_id` (and ideally the epoch/reward-cycle or a per-DB nonce) in `SlotMetadata::auth_digest()`, so a signature over one StackerDB's chunk cannot be replayed into another:
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
This requires threading the contract identifier through `sign`/`verify`/`recover_pk` call sites in `libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs`, and `stackslib/src/net/stackerdb/mod.rs`.

### Proof of Concept
1. Node has two live signer StackerDB contracts for the same reward-cycle parity, e.g. `signers-0-1` and `signers-0-2`, both showing signer `S` at `slot_id = 3` (confirmed identical by `stackerdb-get-signer-slots-page`).
2. Signer `S` legitimately signs and broadcasts chunk `C = (slot_id=3, slot_version=7, data=D)` to `signers-0-1` — this propagates to all peers.
3. Attacker (no keys, unprivileged) captures `C` from the network/gossip or via `GET /v2/stackerdb/.../chunks/3`.
4. Attacker checks that slot 3 in `signers-0-2` currently has version `< 7` (true unless already advanced) and that `7 <= max_writes`.
5. Attacker `POST`s the identical `(slot_id=3, slot_version=7, sig, data=D)` to `signers-0-2`'s chunk endpoint. `SlotMetadata::verify(&addr)` succeeds because `addr` for slot 3 is the same `S` in both contracts, and `try_replace_chunk` accepts it as a fresh, validly-signed chunk for `signers-0-2` — despite `S` never having signed anything for that contract/message type.

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

**File:** stackslib/src/net/stackerdb/db.rs (L398-439)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-223)
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
                    // internal error) is a server error, not a client-classifiable ack, so
                    // it becomes an HTTP 500 rather than a misleading `accepted: false`.
                    let err_code = match &e {
                        NetError::StaleChunk { .. } => StackerDBErrorCodes::DataAlreadyExists,
                        NetError::NoSuchSlot(..) => StackerDBErrorCodes::NoSuchSlot,
                        NetError::BadSlotSigner(..) | NetError::VerifyingError(..) => {
                            StackerDBErrorCodes::BadSigner
                        }
                        NetError::StackerDBChunkTooBig(..) => StackerDBErrorCodes::ChunkTooBig,
                        NetError::TooManySlotWrites { .. } => {
                            StackerDBErrorCodes::TooManySlotWrites
                        }
```

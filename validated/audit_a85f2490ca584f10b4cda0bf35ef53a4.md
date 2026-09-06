## Title
StackerDB chunk signatures omit the target contract ID, enabling cross-contract chunk replay/forgery in signer StackerDBs - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures commit only to `(slot_id, slot_version, data_hash)` — never to the identity of the StackerDB (smart contract) the chunk is destined for. Because the Nakamoto signer-set boot contract (`signers.clar`) assigns the *same* `(signer, slot_id)` mapping to every `signers-{set}-{message_id}` contract for a given signer set, a validly-signed chunk observed on the wire for one message-type StackerDB (e.g. `BlockResponse`) can be relayed or POSTed unmodified into a sibling StackerDB (e.g. `StateMachineUpdate`, `BlockPreCommit`) and will pass signature verification there, exactly as in CVE-2017-16005 where a signature covered header *values* but not header *names/bindings*.

### Finding Description
`SlotMetadata::auth_digest` computes the signed digest as `sha512_256(slot_id || slot_version || data_hash)`, with no contract/DB identifier bound into it: [1](#0-0) 
`SlotMetadata::verify`/`StackerDBChunkData::verify` only check this digest against the recovered public key, again without reference to which StackerDB contract is being written: [2](#0-1) 

Meanwhile, the on-chain slot-assignment logic in `signers.clar` publishes one signer-slot list per signer set (`stackerdb-signer-slots-0`/`-1`), and *every* `signers-{set}-{message_id}` contract (`BlockResponse`, `StateMachineUpdate`, `BlockPreCommit`, etc.) reads that same page: [3](#0-2) 
This is explicitly validated by the test `signers_db_get_slots`, which asserts identical slot listings across every `message_id` in `0..SIGNER_SLOTS_PER_USER` for a given signer set: [4](#0-3) 

Consequently, `get_slot_signer(contract_id, slot_id)` returns the *same* `StacksAddress` for slot N regardless of which `signers-{set}-{message_id}` contract is queried: [5](#0-4) 

The write/validate path (`validate_received_chunk`, used by both sync and unsolicited push handling) checks chunk size, resolves the signer address *for the target contract*, and calls `slot_metadata.verify(&addr)` — but since the digest never included the contract, a chunk legitimately signed for contract A verifies just as well against contract B, provided the same address occupies the same slot in both: [6](#0-5) 

The equality that should hold — *"this specific signature authorizes writing this data to this specific StackerDB slot"* — is broken into *"this signature authorizes writing this data to any StackerDB slot with the same (slot_id, slot_version, data_hash) mapped to the same signer address"*. This is directly analogous to the `http-signature` bug class: content is authenticated, but the binding/"header" (destination contract) is not, so an unprivileged network observer can move a validly-signed payload to an unintended target and have it accepted as authentic there.

The unauthenticated public HTTP endpoint `POST /v2/stackerdb/.../chunks` (`RPCPostStackerDBChunkRequestHandler`) calls `try_replace_chunk` directly with attacker-supplied `contract_identifier` and chunk, requiring only that the chunk pass `slot_metadata.verify`: [7](#0-6) 
and the corresponding p2p write path in `try_replace_chunk` performs the same unbound check before insertion: [8](#0-7) 

### Impact Explanation
Any remote, unprivileged party that observes a validly signed StackerDB chunk on the gossip network (chunks are broadcast in the clear, e.g. via `StackerDBPushChunk`/`process_stacker_db_chunks`, see `stackslib/src/net/relay.rs:2445-2452`) can replay that exact `(slot_id, slot_version, sig, data)` tuple into a different `signers-{set}-{message_id}` StackerDB contract belonging to the same signer set/reward cycle (as long as the required `slot_version` bound is satisfied), and it will be accepted as authentic by any node — no private key needed. This is an unauthorized write to StackerDB state: a signer's `BlockResponse` chunk content can be injected into that signer's `StateMachineUpdate` (or `BlockPreCommit`) slot (or vice versa), corrupting or overwriting what downstream consumers (miner's `StackerDBListener`, `stacks-signer`) treat as that signer's canonical state-machine-update/pre-commit data, and permanently bumping that slot's lamport clock so the legitimate signer cannot easily recover lower version numbers. This can propagate network-wide via the normal chunk broadcast/sync path once accepted by any single peer.

### Likelihood Explanation
No secret key, admin role, or privileged position is required — only observing one legitimately broadcast chunk (trivial, since these are gossiped over the p2p network / visible via RPC) and re-submitting it against a different, predictable sibling contract name (`signers-{set}-{message_id}` naming is public and deterministic). The slot-id-to-signer mapping being identical across message-type contracts is a designed, confirmed behavior (test-asserted), not an edge case.

### Recommendation
Bind the destination StackerDB contract identity into the signed digest, e.g. include `smart_contract` (or a hash of it) inside `SlotMetadata::auth_digest` before signing/verifying, so a signature over one StackerDB's slot cannot be replayed into another. Update `StackerDBChunkData::sign`/`verify`/`recover_pk` accordingly, and add regression tests verifying that a chunk signed for contract A is rejected when submitted against contract B even when the same signer/slot_id/version/data_hash apply.

### Proof of Concept
1. Node observes (via p2p gossip or RPC `GET /v2/stackerdb/<addr>.signers-1-1/chunks/<slot>`) a validly signed `StackerDBChunkData { slot_id: N, slot_version: V, sig, data }` written by signer `S` to `.signers-1-1` (`BlockResponse`).
2. Attacker re-submits the identical struct (same `slot_id`, `slot_version` ≥ current version in target, same `sig`, same `data`) via `POST /v2/stackerdb/<addr>.signers-1-2/chunks` (`StateMachineUpdate`), or relays it as a `StackerDBPushChunk` with `contract_id = .signers-1-2`.
3. `validate_received_chunk`/`try_replace_chunk` resolve `get_slot_signer(.signers-1-2, N) == S` (same mapping as `.signers-1-1`, per `signers.clar`), compute `auth_digest(N, V, data_hash)` — identical to the one used in step 1 — and accept/store the chunk as `S`'s data in the `StateMachineUpdate` slot, even though `S` never signed anything intended for that contract. [9](#0-8) [10](#0-9)

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-244)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }

    /// Sign this slot metadata, committing to slot_id, slot_version, and
    /// data_hash.  Sets self.signature to the signature.
    /// Fails if the underlying crypto library fails
    pub fn sign(&mut self, privkey: &StacksPrivateKey) -> Result<(), Error> {
        let auth_digest = self.auth_digest();
        let sig = privkey
            .sign(&auth_digest.0)
            .map_err(|se| Error::SigningError(se.to_string()))?;

        self.signature = sig;
        Ok(())
    }

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
}

/// Helper methods for StackerDBChunkData messages
impl StackerDBChunkData {
    /// Create a new StackerDBChunkData instance.
    pub fn new(slot_id: u32, slot_version: u32, data: Vec<u8>) -> StackerDBChunkData {
        StackerDBChunkData {
            slot_id,
            slot_version,
            sig: MessageSignature::empty(),
            data,
        }
    }

    /// Calculate the hash of the chunk bytes.  This is the SHA512/256 hash of the data.
    pub fn data_hash(&self) -> Sha512Trunc256Sum {
        Sha512Trunc256Sum::from_data(&self.data)
    }

    /// Create an owned SlotMetadata describing the metadata of this slot.
    pub fn get_slot_metadata(&self) -> SlotMetadata {
        SlotMetadata {
            slot_id: self.slot_id,
            slot_version: self.slot_version,
            data_hash: self.data_hash(),
            signature: self.sig.clone(),
        }
    }

    /// Sign this given chunk data message with the given private key.
    /// Sets self.signature to the signature.
    /// Fails if the underlying signing library fails.
    pub fn sign(&mut self, privk: &StacksPrivateKey) -> Result<(), Error> {
        let mut md = self.get_slot_metadata();
        md.sign(privk)?;
        self.sig = md.signature;
        Ok(())
    }

    pub fn recover_pk(&self) -> Result<StacksPublicKey, Error> {
        let digest = self.get_slot_metadata().auth_digest();
        StacksPublicKey::recover_to_pubkey_without_validating_low_s(digest.as_bytes(), &self.sig)
            .map_err(|ve| Error::VerifyingError(ve.to_string()))
    }

    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
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

**File:** stackslib/src/net/stackerdb/db.rs (L411-437)
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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
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

        Ok(true)
    }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L169-220)
```rust
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
        let http_peer = node.http_peer_addr();

        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
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
```

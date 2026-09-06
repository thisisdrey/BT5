The strongest analog here is a signature/authentication scope-confusion bug: `SlotMetadata`'s signed digest never binds to the StackerDB contract it is destined for, and because signer slot assignment is identical across all per-message-type StackerDB contracts in a reward cycle, a validly-signed chunk from one contract can be replayed into another — the exact "signed/authenticated for context A used in context B" equality break that the ERC-7683 report describes (right signature, wrong destination context).

### Title
Cross-contract StackerDB chunk signature replay due to unbound `auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest for a StackerDB chunk from only `slot_id`, `slot_version`, and `data_hash` — it never incorporates the target StackerDB smart-contract identifier. Because the `.signers-{set}-{message_id}` contracts for a given reward cycle all delegate slot assignment to the same underlying `.signers` list (identical `signer`/`slot_id` mapping across message-id lanes), a signer's valid signature over a chunk destined for one contract (e.g. `signers-0-1`, BlockResponse) is equally valid for the same slot in a sibling contract (e.g. `signers-0-2`, StateMachineUpdate, or `signers-0-3`, BlockPreCommit).

### Finding Description
The signing/verification logic is: [1](#0-0) 

This digest is used both to sign chunks and to verify them in `SlotMetadata::verify`, which is invoked from the StackerDB storage path `StackerDBTx::try_replace_chunk` and from `PeerNetwork::validate_received_chunk`: [2](#0-1) [3](#0-2) 

Both checks resolve the *expected signer address* purely from `(smart_contract, slot_id)` via `get_slot_signer`/`get_slot_validation`, then verify the signature using `SlotMetadata::verify`, which only re-derives `auth_digest()` from `slot_id`/`slot_version`/`data_hash` — the contract identifier plays no role in what is actually signed.

Critically, slot assignment is shared across all message-lane contracts of a reward cycle: each `signers-{set}-{message_id}.clar` contract simply calls back into the shared `.signers` contract for the slot list: [4](#0-3) [5](#0-4) 

So the same `(StacksAddress, slot_id)` mapping is installed by `StackerDBTx::create_stackerdb`/`reconfigure_stackerdb` independently for every message-id contract in the reward cycle: [6](#0-5) 

Given a signer's legitimate, publicly-observable chunk `(slot_id, slot_version, data, sig)` posted to contract A (StackerDB chunks are openly readable/gossiped, no secrecy), any unprivileged remote peer can resubmit the identical tuple to sibling contract B (same reward cycle, different message-id) via the unauthenticated `POST /v2/stackerdb/.../chunks` RPC handler or via unsolicited P2P `StackerDBPushChunk`: [7](#0-6) [8](#0-7) 

`try_replace_chunk`/`validate_received_chunk` only check chunk size, signer-address-vs-slot, monotonic version, and max-writes — they do not check that the signature/data was intended for *this* contract. The signature passes because the digest never bound to contract B in the first place. If accepted (i.e., the replayed `slot_version` exceeds contract B's current version at that slot), the node stores the mismatched data and, per `PeerNetwork::process_stacker_db_chunks`, rebroadcasts it network-wide as a `StackerDBPushChunk`: [9](#0-8) 

This overwrites/clobbers the legitimate slot content for that signer in contract B across the whole network, using only a replayed valid signature the attacker does not control the key for.

### Impact Explanation
High: this allows any unprivileged network peer to force propagation of a signer's cross-contract chunk into the wrong StackerDB replica, network-wide, overwriting the legitimate current value at that slot in the target contract until the real signer publishes a higher version there. This corrupts the state that downstream signer/miner consumers rely on being canonical for that specific message lane (e.g. corrupting the BlockResponse slot with BlockPreCommit payload bytes), a form of serving/propagating non-canonical data as canonical for that StackerDB replica, achieved purely by replay with no key compromise.

### Likelihood Explanation
High: the required signer/slot mapping overlap across sibling `signers-{set}-{message_id}` contracts is a designed, node-wide invariant. Any chunk is trivially observable (chunks are gossiped in cleartext and readable via unauthenticated GET endpoints), and replay requires only a normal, unauthenticated `POST` of the same bytes to a different contract endpoint, or reinjecting it over the P2P gossip channel.

### Recommendation
Bind the signed digest to the destination StackerDB contract identifier (and ideally the target reward cycle/message-id) inside `SlotMetadata::auth_digest()`, e.g. hash `smart_contract.to_string()` (or a fixed-width encoding of it) together with `slot_id`, `slot_version`, and `data_hash`, and thread the contract identifier into `sign`/`verify` call sites (`StackerDBChunkData::sign`/`verify`, `try_replace_chunk`, `validate_received_chunk`) so a signature computed for one contract can never validate for another.

### Proof of Concept
1. During reward cycle `N`, wait for signer `S` (owning slot `k` in both `signers-0-1` and `signers-0-2`) to publish a legitimately signed `StateMachineUpdate` chunk `(slot_id=k, slot_version=v, data=D, sig=Sig)` to `signers-0-2`.
2. Read this chunk (e.g. via the unauthenticated `GET /v2/stackerdb/<signers-0-2>/<k>` RPC, or by observing the `StackerDBPushChunk` gossip message).
3. As an unrelated, unprivileged peer, submit `(slot_id=k, slot_version=v, data=D, sig=Sig)` unchanged to `signers-0-1` (contract with a different `message_id`) via `POST /v2/stackerdb/<signers-0-1>/chunks`, using a `slot_version` value higher than `signers-0-1`'s currently stored version for slot `k`.
4. Observe `StackerDBTx::try_replace_chunk`/`SlotMetadata::verify` accept the chunk (because `auth_digest()` never encoded which contract it was for), the node stores it, and `PeerNetwork::process_stacker_db_chunks` rebroadcasts it as legitimate `signers-0-1` data to the whole network — corrupting that slot for every node without ever needing signer `S`'s private key.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-193)
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L225-269)
```rust
    /// Set up a database's storage slots.
    /// The slots must be in a deterministic order, since they are used to determine the chunk ID
    /// (and thus the key used to authenticate them)
    pub fn create_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        if slots.len() > (STACKERDB_INV_MAX as usize) {
            return Err(net_error::ArrayTooLong);
        }

        if self.get_stackerdb_id(smart_contract).is_ok() {
            return Err(net_error::StackerDBExists(smart_contract.clone()));
        }

        let qry = "INSERT OR REPLACE INTO databases (smart_contract_id) VALUES (?1)";
        let mut stmt = self.sql_tx.prepare(qry)?;
        let args = params![smart_contract.to_string()];
        stmt.execute(args)?;

        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;

        let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
        let mut stmt = self.sql_tx.prepare(qry)?;
        let mut slot_id = 0u32;

        for (principal, slot_count) in slots.iter() {
            test_debug!("Create StackerDB slots: ({}, {})", &principal, slot_count);
            for _ in 0..*slot_count {
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];
                stmt.execute(args)?;

                slot_id += 1;
            }
        }
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-438)
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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-767)
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-237)
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
                        _ => {
                            error!("Failed to replace StackerDB chunk with an unexpected error";
                                   "smart_contract_id" => contract_identifier.to_string(),
                                   "error" => format!("{:?}", &e)
                            );
                            return Err(StacksHttpResponse::new_error(
                                &preamble,
                                &HttpServerError::new(format!(
                                    "Failed to store StackerDB chunk for {}: {:?}",
                                    &contract_identifier, &e
                                )),
                            ));
                        }
                    };
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

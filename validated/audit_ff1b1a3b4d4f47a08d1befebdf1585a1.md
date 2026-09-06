### Title
Cross-contract StackerDB chunk signature replay allows forged/mis-typed data to be stored and propagated network-wide - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures only commit to `(slot_id, slot_version, data_hash)` and never bind the target StackerDB's smart-contract identifier. Because the Stacks node deploys many *sibling* StackerDB contracts (`signers-0-{message_id}` / `signers-1-{message_id}`) that all derive their slot→signer assignment from the exact same underlying signer list (`stackerdb-get-signer-slots-page`), a given signer occupies the identical `slot_id` in every sibling contract for its reward cycle. A validly signed chunk produced for one message-type StackerDB is therefore also a validly signed chunk for every other sibling StackerDB the same signer participates in, enabling replay/type-confusion of signer messages across contracts, which then get accepted, stored, and re-broadcast to the whole network as legitimate.

### Finding Description
The signed digest for a StackerDB chunk is computed purely from local slot state, with no domain separation on the StackerDB contract: [1](#0-0) 

`SlotMetadata::verify` recovers the public key strictly from this digest and compares only the resulting `Hash160` against a supplied `principal` — it has no notion of *which* StackerDB the signature is supposed to belong to: [2](#0-1) 

Server-side, the "owner" check is contract-scoped only through the lookup of `slot_validation.signer`/`get_slot_signer`, which is keyed by `(smart_contract, slot_id)`: [3](#0-2) [4](#0-3) 

The problem is that this per-contract lookup resolves to the *same* signer address across sibling contracts, because every `signers-{set}-{message_id}` contract fetches its slot list from the identical page in the shared `.signers` contract: [5](#0-4) [6](#0-5) 

Since the *order* in which signers are enumerated (and thus the resulting `slot_id` assignment produced by `create_stackerdb`/`reconfigure_stackerdb`) is deterministic and identical for all `message_id` variants sharing the same `signer_set` page, signer `S` who owns `slot_id = X` in `signers-0-0` also owns `slot_id = X` in `signers-0-1`, `signers-0-2`, etc. Because the signature (`auth_digest`) never includes the contract identifier, a `StackerDBChunkData{slot_id: X, slot_version: V, sig, data}` signed for `signers-0-0` will pass `verify()`/`try_replace_chunk` unmodified when POSTed to `signers-0-1` (as long as slot X's version/write-count constraints in the target contract also allow it). This equality break — "signed-for-contract-A" treated as "valid-for-contract-B" — is the direct structural analog of the XML wrapping/decryption confusion in the reported CVE: content authenticated in one context is accepted as authentic in a different context because the signature does not bind the context.

The write path is reachable remotely and unauthenticated (no additional secret is needed beyond the signer's own key, which they legitimately hold for their own reward-cycle slot): [7](#0-6) 

and once stored, the chunk is unconditionally broadcast to the whole p2p network and forwarded to the local event observer / signer runloop: [8](#0-7) [9](#0-8) 

Downstream consumers such as `StackerDBListener` decode the chunk bytes as the message type expected for that specific contract/slot and act on the (now foreign) content: [10](#0-9) 

### Impact Explanation
A signer can take a chunk that they legitimately signed for one message-type StackerDB and replay it verbatim into a sibling StackerDB contract where they hold the same slot, bypassing the intended per-contract message-type separation. The receiving node's signature check succeeds (same signer, same slot, same digest bytes), so the replayed/foreign-context data is accepted as canonical for the target contract, stored, and propagated network-wide via `broadcast_message`, and interpreted by consumers (e.g. `StackerDBListener`, event observers) as legitimate content for that DB/slot. This can inject or reorder/replay signer protocol messages (e.g. re-submitting a stale but validly-signed payload into a different message channel), corrupting downstream node state derived from StackerDB content and propagating that corruption to the rest of the network — matching "network-wide propagation of forged data" / non-canonical data served as canonical.

### Likelihood Explanation
Exploitation requires only being a currently-registered signer (a role held by potentially dozens/hundreds of parties per reward cycle) and does not require anyone else's key or admin access. The multiple sibling `signers-{set}-{message_id}` contracts sharing one underlying slot assignment is an intentional, unconditional design (visible directly in the `.clar` boot contracts), so the precondition (same `slot_id` across siblings) always holds for every signer, making this a reliably reproducible primitive rather than a rare edge case.

### Recommendation
Bind the StackerDB smart-contract identifier (and ideally reward-cycle/signer-set) into the signed digest in `SlotMetadata::auth_digest` (e.g., include `smart_contract_id` bytes in the hasher before slot_id/slot_version/data_hash), and bump/version the signing scheme so old signatures are rejected under the new digest. Alternatively, have `try_replace_chunk`/`validate_received_chunk` verify against a digest that already incorporates the contract context, and reject any digest that does not match the (contract, slot, version, hash) tuple exactly.

### Proof of Concept
1. As a registered signer for reward cycle `R`, sign a `StackerDBChunkData` payload (any bytes) with `slot_id = X`, `slot_version = V`, over the `.signers-0-0` contract (this signer owns slot X there).
2. Take the identical serialized `StackerDBChunkData` (same `sig`, `slot_id`, `slot_version`, `data`) and POST it to the RPC endpoint for `.signers-0-1` (or any other `signers-0-*` contract) instead, per [11](#0-10) .
3. Because slot X is owned by the same signer in `.signers-0-1` (same underlying signer list), `try_replace_chunk`'s `slot_desc.verify(&slot_validation.signer)` check succeeds (`stackslib/src/net/stackerdb/db.rs:411-423`), the chunk is stored, and `RPCPostStackerDBChunkRequestHandler` triggers a network-wide `StackerDBPushChunk` broadcast of this now cross-context data (`stackslib/src/net/api/poststackerdbchunk.rs:315-324`), which peers accept via the same signature-only check in `PeerNetwork::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:679-697`).

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L155-176)
```rust
impl RPCRequestHandler for RPCPostStackerDBChunkRequestHandler {
    /// Reset internal state
    fn restart(&mut self) {
        self.contract_identifier = None;
        self.chunk = None;
    }

    /// Make the response.
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
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

**File:** stackslib/src/net/relay.rs (L2340-2380)
```rust
    pub fn process_uploaded_stackerdb_chunks(
        &mut self,
        rc_consensus_hash: &ConsensusHash,
        uploaded_chunks: Vec<StackerDBPushChunkData>,
        event_observer: Option<&dyn StackerDBEventDispatcher>,
    ) {
        let mut all_events: HashMap<QualifiedContractIdentifier, Vec<StackerDBChunkData>> =
            HashMap::new();
        for chunk in uploaded_chunks.into_iter() {
            // Always forward the event to ensure the local signer receives it.
            if event_observer.is_some() {
                if let Some(events) = all_events.get_mut(&chunk.contract_id) {
                    events.push(chunk.chunk_data.clone());
                } else {
                    all_events.insert(chunk.contract_id.clone(), vec![chunk.chunk_data.clone()]);
                }
            }

            if chunk.rc_consensus_hash != *rc_consensus_hash {
                debug!("Not rebroadcasting stale uploaded StackerDB chunk";
                           "stackerdb_contract_id" => %chunk.contract_id,
                           "slot_id" => chunk.chunk_data.slot_id,
                           "slot_version" => chunk.chunk_data.slot_version,
                           "chunk.rc_consensus_hash" => %chunk.rc_consensus_hash,
                           "network.rc_consensus_hash" => %rc_consensus_hash);
                continue;
            }

            debug!("Got uploaded StackerDB chunk"; "stackerdb_contract_id" => %chunk.contract_id, "slot_id" => chunk.chunk_data.slot_id, "slot_version" => chunk.chunk_data.slot_version);

            let msg = StacksMessageType::StackerDBPushChunk(chunk);
            if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                warn!("Failed to broadcast StackerDB chunk: {e:?}");
            }
        }
        if let Some(observer) = event_observer {
            for (contract_id, new_chunks) in all_events.into_iter() {
                observer.new_stackerdb_chunks(contract_id, new_chunks);
            }
        }
    }
```

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L372-423)
```rust
            for (slot_id, _pk, message) in messages.into_iter() {
                let Some(signer_entry) = &self.signer_entries.get(&slot_id) else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Signer entry not found".into(),
                    ));
                };
                let Ok(signer_pubkey) = StacksPublicKey::from_slice(&signer_entry.signing_key)
                else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Failed to parse signer public key".into(),
                    ));
                };

                match message {
                    SignerMessageV0::BlockResponse(BlockResponse::Accepted(accepted)) => {
                        let BlockAccepted {
                            signer_signature_hash: block_sighash,
                            signature,
                            metadata,
                            response_data,
                        } = accepted;
                        let tenure_extend_timestamp = response_data.tenure_extend_timestamp;
                        let read_count_extend_timestamp =
                            response_data.tenure_extend_read_count_timestamp;

                        let (lock, cvar) = &*self.blocks;
                        let mut blocks = lock.lock().expect("FATAL: failed to lock block status");

                        let Some(block) = blocks.get_mut(&block_sighash) else {
                            info!(
                                "StackerDBListener: Received signature for block that we did not request. Ignoring.";
                                "signature" => %signature,
                                "signer_signature_hash" => %block_sighash,
                                "slot_id" => slot_id,
                                "signer_set" => self.signer_set,
                            );
                            continue;
                        };

                        let Ok(valid_sig) = signer_pubkey.verify(block_sighash.bits(), &signature)
                        else {
                            warn!(
                                "StackerDBListener: Got invalid signature from a signer. Ignoring."
                            );
                            continue;
                        };
                        if !valid_sig {
                            warn!(
                                "StackerDBListener: Processed signature but didn't validate over the expected block. Ignoring";
                                "signature" => %signature,
                                "signer_signature_hash" => %block_sighash,
                                "slot_id" => slot_id,
```

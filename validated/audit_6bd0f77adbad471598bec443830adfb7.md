Confirmed: the HTTP POST endpoint `try_handle_request` in `stackslib/src/net/api/poststackerdbchunk.rs` calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), &stackerdb_chunk.data)` with the same context-free `SlotMetadata::verify` path, and on success it even auto-relays the chunk over the p2p network via `node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))` — so a successful cross-contract replay also causes the forged/misdirected write to propagate network-wide.

### Title
StackerDB chunk signatures omit the contract identifier, enabling cross-DB replay of validly-signed chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `(slot_id, slot_version, data_hash)`, with no binding to the StackerDB contract (or network) the chunk is destined for. Because Nakamoto reward-cycle signer StackerDB contracts (`signers-{0,1}-{0..7}`) all derive their `(signer, slot_id)` assignment from the exact same underlying list (`stackerdb-get-signer-slots-page`), the same signer address owns the same `slot_id` across every message-type contract for a cycle. Consequently a chunk legitimately signed and broadcast for one contract (e.g. `.signers-0-1`, BlockResponse) is a valid, verifiable chunk for a *different* contract (e.g. `.signers-0-6`, StateMachineUpdate) that the same signer also owns slot N in — with no need for the signer's private key at all, since chunks are public/gossiped data.

### Finding Description
`SlotMetadata::auth_digest` at [1](#0-0)  hashes only `slot_id`, `slot_version`, and `data_hash`. `SlotMetadata::verify` at [2](#0-1)  recovers the public key from that digest and compares its hash to the expected principal — it never checks which smart contract the chunk is claimed to belong to.

The actual "is this the right signer for this slot" check is done separately, per target contract, in `validate_received_chunk`: [3](#0-2)  and in `StackerDBTx::try_replace_chunk` (used by both the HTTP POST handler and p2p handling), which look up the slot owner *for the target contract* via `get_slot_signer`/`get_slot_validation`, then call `slot_metadata.verify(&addr)`. Since `verify` never authenticates the contract, any chunk that recovers to the correct address for that contract's slot table is accepted — regardless of which contract the signature was originally produced for.

By design, every per-message-type StackerDB contract for a reward cycle (`signers-0-0` .. `signers-0-7`, and `signers-1-0` .. `signers-1-7`) shares the identical signer→slot assignment, since each calls back into `.signers`' `stackerdb-get-signer-slots-page`: [4](#0-3)  and [5](#0-4) . Thus, for a given reward cycle, signer X occupies the same `slot_id` in all 8 message-type contracts.

The HTTP write path in `RPCPostStackerDBChunkRequestHandler::try_handle_request` accepts any well-formed `StackerDBChunkData` from an unauthenticated remote HTTP client and, on success, automatically relays it network-wide via `node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))`: [6](#0-5) . The unsolicited p2p push path (`handle_unsolicited_StackerDBPushChunk`) performs the same context-free signature check.

This exactly mirrors the reported bug class: signature schemes that omit any binding to the specific "contract instance" allow signatures to be replayed across instances that share the same signer/key material.

### Impact Explanation
Any unauthenticated network participant who observes a validly-signed chunk broadcast for one signer StackerDB contract (chunks are public — they are relayed/gossiped and directly downloadable via the DB sync protocol and RPC `GET`) can resubmit the identical `(slot_id, slot_version, sig, data)` tuple to a *different* StackerDB contract in which the same signer owns the same slot, causing that contract's slot to be overwritten with attacker-chosen (but originally signer-authored) content that the signer never intended to write there. This is an unauthorized write into StackerDB state, and it is automatically re-propagated to the whole network via the relay-on-accept logic in the POST-chunk handler, satisfying the "Critical: unauthenticated/unauthorized write to state or StackerDB; network-wide propagation of forged data" bar. It also creates cross-contract confusion (e.g., "poisoning" the version counter of the target contract's slot, potentially blocking or delaying legitimate future writes from that signer for that slot until the version catches up), and can be used to inject stale/misattributed data into node-critical structures such as the `StateMachineUpdate` or `BlockResponse` slot tracked by `StackerDBListener`.

### Likelihood Explanation
Exploitation requires no privileged access, no private key, and no cooperation from the impersonated signer — only observation of one publicly-broadcast chunk and a single unauthenticated HTTP POST (or p2p push) to a different contract endpoint where the slot-ownership coincidence (guaranteed by the `.signers` boot contract design) holds. The only constraint is that the target contract's slot version must not have already advanced past the replayed chunk's version, which is trivially achievable near the start of a reward cycle or against a currently idle message-type slot (e.g., `MockProposal`/`MockSignature`/`BlockPreCommit` slots that are written to less frequently).

### Recommendation
Include the target `QualifiedContractIdentifier` (and ideally the network/chain identifier, e.g. `mainnet`/`testnet` flag or `rc_consensus_hash`) inside the signed digest computed by `SlotMetadata::auth_digest`, so a chunk signature is only valid for the specific StackerDB contract (and network context) it was produced for. This requires a coordinated version bump across `libstackerdb`, all StackerDB clients (`stacks-signer`, `stacks-node` miner/signer coordinators), and the on-chain/off-chain verification logic in `stackslib/src/net/stackerdb`.

### Proof of Concept
1. Signer X is assigned slot `N` in both `.signers-0-1` (BlockResponse) and `.signers-0-6` (StateMachineUpdate) for the current reward cycle (guaranteed, since both read the same slot list from `.signers`).
2. Signer X legitimately signs and pushes a `StackerDBChunkData{slot_id: N, slot_version: V, data: D}` to `.signers-0-1` via the normal signer flow (e.g. `send_message_bytes_with_retry` in `stacks-signer/src/client/stackerdb.rs`); this chunk is gossiped/relayed and is retrievable by any peer.
3. An attacker with no keys, simply having observed this chunk, issues `POST /v2/stackerdb/<addr>/signers-0-6/chunks` with the identical `{slot_id: N, slot_version: V', sig, data: D}` body (`V'` chosen to be `>=` the current expected version for slot `N` in `.signers-0-6`, e.g. 0 near cycle start).
4. `try_replace_chunk` → `slot_metadata.verify(&addr)` succeeds (digest doesn't encode the contract), version/size/write-count checks pass, and the chunk is stored in `.signers-0-6`'s slot `N` and relayed network-wide via `StackerDBPushChunk`, even though signer X never wrote or authorized this content for `.signers-0-6`.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-323)
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

                    // Load the current slot metadata to populate the ack for the client.
                    let slot_metadata_opt =
                        match tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id) {
                            Ok(slot_opt) => slot_opt,
                            Err(e) => {
                                // some other error
                                error!("Failed to load replaced StackerDB chunk metadata";
                                       "smart_contract_id" => contract_identifier.to_string(),
                                       "error" => format!("{:?}", &e)
                                );
                                return Err(StacksHttpResponse::new_error(
                                    &preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to load StackerDB chunk for {}: {:?}",
                                        &contract_identifier, &e
                                    )),
                                ));
                            }
                        };

                    let reason = serde_json::to_string(&err_code.clone().into_json())
                        .unwrap_or("(unable to encode JSON)".to_string());

                    let ack = StackerDBChunkAckData {
                        accepted: false,
                        reason: Some(reason),
                        metadata: slot_metadata_opt,
                        code: Some(err_code.code()),
                    };
                    return Ok(ack);
                }

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

                if let Err(e) = tx.commit() {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(format!("Failed to commit StackerDB tx: {:?}", &e)),
                    ));
                }

                crate::net::stackerdb::log_stored_stackerdb_chunk(
                    &contract_identifier,
                    &stackerdb_chunk,
                    &crate::net::stackerdb::StackerDBChunkOrigin::Http { peer: http_peer },
                );

                // success!
                let ack = StackerDBChunkAckData {
                    accepted: true,
                    reason: None,
                    metadata: Some(slot_metadata),
                    code: None,
                };

                return Ok(ack);
            });

        let ack_resp = match ack_resp {
            Ok(ack) => ack,
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

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

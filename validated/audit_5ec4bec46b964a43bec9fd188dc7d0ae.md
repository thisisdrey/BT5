### Title
StackerDB chunk signatures are not bound to a specific StackerDB contract, enabling cross-channel chunk replay and write-budget griefing - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures authenticate only `(slot_id, slot_version, data_hash)` and never the target StackerDB contract identifier. Because the Stacks node assigns the *same* signer the *same* `slot_id` across all sibling `.signers-{set}-{message_id}` StackerDB contracts for a reward cycle, a validly-signed chunk published to one channel can be replayed verbatim by any unprivileged remote party into a different, unrelated channel where that signer owns the same slot, as long as the freshness check on the target channel is satisfied. This is a direct analog of the reported "replaying existing requests" / "piggybacking off existing channels" / "griefing existing channels" bug class.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash` — it never includes the StackerDB's `smart_contract` identifier: [1](#0-0) 

`SlotMetadata::verify()` / `StackerDBChunkData::verify()` recover the public key from that digest and only check that the recovered key hash equals the *slot owner address* — again with no reference to which contract the signature was produced for: [2](#0-1) 

On the write path, `StackerDBTx::try_replace_chunk` accepts a chunk if `slot_desc.verify(&slot_validation.signer)` succeeds for *that contract's* slot table and the version is fresh — but since the signature contains no contract binding, this check succeeds for any contract in which the signer happens to own the same `slot_id`: [3](#0-2) 

Slot assignment is deterministic and identical across the sibling message-type contracts for a signer set/reward cycle — the reward-cycle signer ordering (sorted by public key) is reused for every `message_id` in `0..SIGNER_SLOTS_PER_USER`, so the same signer occupies the same `slot_id` in `signers-0-0`, `signers-0-1`, `signers-0-2`, etc.: [4](#0-3) 

This is reinforced on the client side: the signer's local monotonic version counter is tracked purely by `(signer_pubkey, slot_id)`, without any notion of which StackerDB contract it belongs to — showing the version namespace is not designed to be contract-scoped: [5](#0-4) 

The HTTP write endpoint (`POST /v2/stackerdb/:address/:contract/chunks`) requires no authentication beyond the embedded chunk signature and accepts any well-formed `StackerDBChunkData` for the target contract named in the URL: [6](#0-5) 

Because both StackerDB chunk contents and gossip (`StackerDBPushChunk`) are public, and slot assignment is deterministic/known, any remote unprivileged party can:
1. Observe a legitimately signed `StackerDBChunkData` (signature, slot_id, slot_version, data) that signer S published to contract A (e.g. via HTTP GET, or via passive P2P observation of `StackerDBPushChunk` gossip).
2. Replay that exact `(sig, slot_id, slot_version, data)` tuple to a different contract B (`POST /v2/stackerdb/.../chunks`) where S also owns `slot_id` (guaranteed within a signer_set/reward cycle).
3. `try_replace_chunk` for contract B validates the signature against B's slot owner (which is S, same address) and accepts it as long as B's stored `slot_version` for that slot is lower than the replayed one — trivially true against a freshly-reset or lagging channel.

### Impact Explanation
This is an unauthenticated write into a StackerDB slot that the account never chose to write to via that channel:
- **Wrong-channel data injection**: data intended for one signer message type (e.g. `BlockResponse`) can be forced into an unrelated channel (e.g. `Transactions`), corrupting whatever consumer reads that slot as canonical current state for that signer/channel.
- **Griefing / write-budget exhaustion**: the replay advances the target channel's stored `slot_version` to an attacker-chosen value (bounded only by `max_writes`), while the signer's own local write-tracking is oblivious to this cross-channel poisoning. Any subsequent legitimate write by the actual signer with a lower version is rejected as `StaleChunk`/`TooManySlotWrites` (`stackslib/src/net/stackerdb/db.rs:424-436`), durably degrading or permanently locking that signer out of writing to the poisoned channel — no private key required by the attacker.
- This maps to "unauthenticated/unauthorized write to state or StackerDB" and forged-data propagation (the poisoned chunk is also picked up and relayed via `StackerDBPushChunk` gossip to the whole network), placing it in the Critical impact bucket per the validation rules.

### Likelihood Explanation
High. No secret material is required — only a previously observed, legitimately-signed chunk (StackerDB data/metadata is intentionally public) and knowledge of the deterministic slot assignment shared across sibling `.signers-*` contracts for a reward cycle (itself publicly queryable via `stackerdb-get-signer-slots`). The write endpoint is unauthenticated by design (any peer can `POST` a chunk), so exploitation requires only network access to a node's RPC/P2P interface.

### Recommendation
Bind the signed digest to the destination StackerDB contract identity (e.g., include the `smart_contract` `QualifiedContractIdentifier` in `SlotMetadata::auth_digest()`), so a chunk signature is only valid for the specific StackerDB it was produced for. This closes the cross-channel replay path while preserving backward-compatible slot/version semantics per channel.

### Proof of Concept
1. Let signer `S` (private key unknown to attacker) legitimately sign and publish chunk `C = (slot_id=k, slot_version=5, data=D, sig=SIG)` to StackerDB contract `A` (`signers-0-1`), where `verify()` succeeds because `S` owns slot `k` in `A`.
2. Attacker fetches `C` (public GET endpoint or passive P2P observation of the `StackerDBPushChunk` gossip for contract `A`).
3. Attacker confirms (via public `stackerdb-get-signer-slots` calls) that `S` also owns slot `k` in sibling contract `B` (`signers-0-2`), and that `B`'s currently stored version for slot `k` is `< 5`.
4. Attacker sends `POST /v2/stackerdb/<B-address>/<B-contract>/chunks` with the exact same `(slot_id=k, slot_version=5, sig=SIG, data=D)` from `C`.
5. `try_replace_chunk` for `B` calls `slot_desc.verify(&slot_validation.signer)`, which succeeds (same address `S` owns slot `k` in `B`, and the digest check only depends on `slot_id/version/data_hash`, not on contract `B`), and the version-freshness check passes since `B`'s stored version is behind. The chunk is accepted, stored, and rebroadcast as `B`'s current chunk for slot `k`, without `S` ever having authorized or produced a write to `B`.

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

**File:** stacks-signer/src/client/stackerdb.rs (L179-185)
```rust
        let signer_pk = StacksPublicKey::from_private(&self.stacks_private_key);
        loop {
            let slot_version = self
                .signer_db
                .get_latest_chunk_version(&signer_pk, slot_id.0)?
                .map(|x| x.saturating_add(1))
                .unwrap_or(0);
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L163-201)
```rust
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
```

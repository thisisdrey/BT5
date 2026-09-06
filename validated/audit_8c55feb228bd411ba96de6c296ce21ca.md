### Title
StackerDB chunk-authentication digest omits the smart-contract identifier, enabling cross-replica signature replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest()` — the digest that `SlotMetadata::sign`/`verify` and `StackerDBChunkData::sign`/`verify` operate over — is computed only from `slot_id`, `slot_version`, and `data_hash`. It does not include the `QualifiedContractIdentifier` of the StackerDB replica the chunk is destined for. Consequently, a validly-signed chunk that a signer produced for one StackerDB contract is also a validly-signed chunk for any other StackerDB contract in which that same signer happens to occupy the same `slot_id`, with no cooperation from the signer required to "authorize" the second use.

### Finding Description
The signed digest is built like this: [1](#0-0) 

and verification recovers the pubkey solely from this contract-agnostic digest: [2](#0-1) 

On the write path, `StackerDBs::try_replace_chunk` looks up the *expected* signer for `(smart_contract, slot_id)` and calls `slot_desc.verify(&slot_validation.signer)`, but the thing being verified — the digest itself — never mentions `smart_contract`: [3](#0-2) 

The same contract-agnostic check is used on the read/relay side, in `PeerNetwork::validate_received_chunk`, which is invoked both for chunks pulled during sync and for unsolicited pushed chunks (`handle_unsolicited_StackerDBPushChunk`): [4](#0-3) 

and on the HTTP RPC write endpoint `POST /v2/stackerdb/{principal}/{contract}/chunks`, which calls `try_replace_chunk` directly: [5](#0-4) 

This is exactly the equality that the external report's bug class targets: the thing that is *authenticated* (a signature over `slot_id || slot_version || data_hash`) is weaker than the thing that is *stored/served* (a chunk bound to a specific `smart_contract` replica). Any unprivileged party who observes a chunk broadcast/GET response for contract `A` can relay the identical `StackerDBChunkData` bytes (slot_id, slot_version, sig, data unchanged) to a different contract `B`'s POST-chunk endpoint or push it over the p2p wire. If `B` independently assigned the same signer address to the same `slot_id` (a realistic case for Nakamoto signer-set StackerDBs, where multiple `.signers-<cycle>-<page>` contracts are configured from overlapping/identical signer sets and slot indices), `B`'s replica will accept and store this stale/foreign-context data as if the signer legitimately wrote it into `B`, with no participation by the signer for that specific write.

### Impact Explanation
This lets an unauthenticated network participant cause a StackerDB replica to store and re-broadcast data the signer never intended for that contract/slot — a form of unauthenticated write / forged-data propagation across a StackerDB instance, since the write succeeds purely by relaying previously-observed, differently-scoped signed bytes. Depending on which off-chain protocol consumes that StackerDB's contents (e.g., Nakamoto block-signing StackerDBs), this can result in stale or wrong data being served/propagated as canonical for that replica's slot.

### Likelihood Explanation
Exploitability requires only passive observation of a chunk that is already broadcast on the p2p network or fetchable via the public `GET`/`POST` StackerDB chunk RPCs, plus a target contract that assigns the same signer to the same slot index — a configuration pattern that is plausible given how multiple StackerDB replicas are created from similar/overlapping signer sets across reward cycles. No secret key, node privilege, or admin role is required by the attacker; only relaying observed wire bytes.

### Recommendation
Bind the signed digest to the specific StackerDB instance by including the `QualifiedContractIdentifier` (or equivalently a `stackerdb_id`) in `SlotMetadata::auth_digest()`, and mirror this in `StackerDBChunkData::sign`/`verify` and all verification call sites (`try_replace_chunk`, `validate_received_chunk`, `poststackerdbchunk.rs`). This requires a wire/format migration for `SlotMetadata`/`StackerDBChunkData` signing.

### Proof of Concept
1. Configure two StackerDB contracts, `A` and `B`, such that address `S` owns `slot_id = 0` in both (achievable whenever both contracts derive their signer-to-slot mapping from the same or overlapping signer sets, e.g. adjacent reward-cycle `.signers-*` contracts as created via `tx.create_stackerdb`, see [6](#0-5) ).
2. Signer `S` legitimately signs and posts `StackerDBChunkData { slot_id: 0, slot_version: 1, sig, data }` to contract `A` via `POST /v2/stackerdb/{A}/chunks`; it is accepted (`try_replace_chunk` succeeds) and rebroadcast via `StackerDBPushChunk`.
3. Any observer captures this exact `StackerDBChunkData` (unmodified bytes) from the p2p broadcast or from `GET` on `A`'s replica.
4. The observer submits the identical bytes to contract `B` via `POST /v2/stackerdb/{B}/chunks` (or as an unsolicited `StackerDBPushChunk` p2p message).
5. `validate_received_chunk`/`try_replace_chunk` for `B` calls `slot_desc.verify(&slot_validation.signer)` where `slot_validation.signer == S` for `B`'s slot 0 as well; since `auth_digest()` never references the contract, verification succeeds and `B` stores/accepts data that `S` never signed for `B`.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-220)
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
```

**File:** stackslib/src/net/stackerdb/tests/db.rs (L364-373)
```rust
    tx.create_stackerdb(
        &sc,
        &addrs
            .clone()
            .into_iter()
            .map(|addr| (addr, 1))
            .collect::<Vec<_>>(),
    )
    .unwrap();

```

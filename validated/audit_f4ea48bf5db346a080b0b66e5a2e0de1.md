This confirms the vulnerability fully. The `try_replace_chunk` path at `stackslib/src/net/stackerdb/db.rs` calls `slot_desc.verify(&slot_validation.signer)` using `SlotMetadata::verify` from `libstackerdb/src/libstackerdb.rs`, whose signature check is based solely on `auth_digest()`, which never mixes in `smart_contract`. The RPC endpoint `RPCPostStackerDBChunkRequestHandler::try_handle_request` in `stackslib/src/net/api/poststackerdbchunk.rs` passes the attacker-controlled `contract_identifier` from the URL path directly to `try_replace_chunk`, but that value is never part of what's hashed/signed. If the same signer key legitimately owns slot 0 in two different StackerDB-backed contracts (A and B) on the same node/network, a `(slot_id, slot_version, sig)` triple signed for contract A's slot 0 also validates for contract B's slot 0, since both computations of `auth_digest()` are identical.

### Title
Cross-contract StackerDB chunk replay via contract-unbound signature digest - (`libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` in `libstackerdb/src/libstackerdb.rs` computes the signed digest over only `slot_id`, `slot_version`, and `data_hash`, omitting the StackerDB's smart contract identifier. Because `StackerDBTx::try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs` authenticates a chunk solely via `slot_desc.verify(&slot_validation.signer)`, a chunk signed for slot 0 of contract A is equally valid for slot 0 of contract B whenever the same key is the configured signer for that slot in both contracts.

### Finding Description
The intended equality for authenticating a StackerDB chunk write should bind the signature to `(contract_id, slot_id, slot_version, data_hash)`, but `SlotMetadata::auth_digest` only hashes `slot_id.to_be_bytes() || slot_version.to_be_bytes() || data_hash.0` [1](#0-0) . `SlotMetadata::sign`/`verify` operate purely on this digest [2](#0-1) , and `StackerDBChunkData::sign`/`verify` simply delegate to it [3](#0-2) .

On the write path, `RPCPostStackerDBChunkRequestHandler::try_handle_request` takes the `contract_identifier` from the attacker-controlled URL path and the chunk body, and calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), &stackerdb_chunk.data)` [4](#0-3) . Inside `try_replace_chunk`, the only authentication check is `slot_desc.verify(&slot_validation.signer)` where `slot_validation.signer` is looked up per-contract-per-slot, but the verified digest itself carries no contract binding [5](#0-4) .

Exploit flow: attacker owns slot 0 in contract A and also slot 0 in contract B (same key configured as signer for both, which is plausible/legitimate e.g. same operator running multiple StackerDB replicas). Attacker signs `StackerDBChunkData{slot_id:0, slot_version:V, data}` for contract A normally. The resulting `sig` is valid input to `verify()` regardless of which contract's slot_validation.signer matches, because `auth_digest` is contract-agnostic. Attacker then POSTs the identical `slot_id`, `slot_version`, `sig`, and `data` to `/v2/stackerdb/<addrB>/<contractB>/chunks`. `try_replace_chunk` recomputes the same digest, `verify` succeeds against B's signer (same key), version check passes, and the chunk is written into contract B's replica and gossiped via `StackerDBPushChunk` to the network [6](#0-5) .

### Impact Explanation
This allows a chunk cryptographically intended for one StackerDB instance/contract to be accepted and stored as valid, correctly-signed data in a different StackerDB instance, then relayed network-wide as a legitimate `StackerDBPushChunk`. Since downstream consumers (e.g. signer message processing) trust that a chunk stored under contract B's slot was actually authorized for contract B's semantic context, this breaks per-contract isolation and can result in unauthenticated/misattributed data being accepted as canonical for contract B — matching "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" (Critical).

### Likelihood Explanation
Precondition is that the same private key is configured as the owner of slot 0 (or any slot at the same index) in two different StackerDB contracts on the same node/network — a configuration that is plausible in practice since node operators and signers often reuse the same keys across contracts/reward cycles. No secret or privileged role is needed; the attacker only needs to be the legitimate holder of a slot in contract A and to know that the same key is also a slot signer in contract B, then send one crafted HTTP POST reachable at the node's public RPC port. It's fully repeatable for any subsequent chunk version.

### Recommendation
Include the `smart_contract` (`QualifiedContractIdentifier`) — or at least its serialized issuer+name — as part of the hashed material in `SlotMetadata::auth_digest`, requiring `sign`/`verify` callers to pass the contract identifier explicitly, and updating `try_replace_chunk` to pass the contract into `slot_desc.verify(contract_identifier, &slot_validation.signer)`.

### Proof of Concept
Rust test in `stackslib/src/net/stackerdb/tests/db.rs`:
1. Create two StackerDB contracts, A and B, each with slot 0 owned by the same `StacksPrivateKey`/`StacksAddress` via `create_stackerdb`.
2. Build a `StackerDBChunkData{slot_id:0, slot_version:1, data: b"hello".to_vec()}`, call `.sign(&privkey)` to produce `sig`.
3. Call `tx.try_replace_chunk(&contract_A, &chunk.get_slot_metadata(), &chunk.data)` — expect `Ok(())`.
4. Reuse the exact same `chunk` (same `slot_id`, `slot_version`, `sig`, `data`) and call `tx.try_replace_chunk(&contract_B, &chunk.get_slot_metadata(), &chunk.data)`.
5. Assert this second call also returns `Ok(()))`, demonstrating that a chunk signed and validated for contract A is accepted verbatim by contract B, confirming the cross-contract replay.

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

**File:** libstackerdb/src/libstackerdb.rs (L171-193)
```rust
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

**File:** libstackerdb/src/libstackerdb.rs (L226-244)
```rust
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
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

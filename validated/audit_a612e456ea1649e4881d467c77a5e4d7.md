### Title
StackerDB chunk signatures omit the contract ID, enabling cross-StackerDB replay of validly-signed chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the digest that a StackerDB writer signs over as `sha512/256(slot_id || slot_version || data_hash)`. It never incorporates the StackerDB's smart-contract identifier (the "contract address" in the report's terminology) or any network/chain discriminator. Because the same signer address (e.g., a Stacks signer) commonly owns the *same numeric slot index* across multiple, distinct StackerDB replicas (e.g., different `.signers-<cycle>-<set>` contracts across reward cycles, or other signer-message contracts), a chunk that was legitimately signed and accepted into one StackerDB contract can be replayed byte-for-byte into a different StackerDB contract, where it will verify successfully.

### Finding Description
`SlotMetadata::auth_digest()` binds only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`sign()`/`verify()` operate purely over this digest, with no reference to which StackerDB (contract) the chunk belongs to: [2](#0-1) 

Chunk validation on the receiving node (`validate_received_chunk`, used both by unsolicited push-chunk handling and by StackerDB sync) looks up the expected signer purely from `(smart_contract_id, slot_id)` and then calls `slot_metadata.verify(&addr)`, which — as shown above — checks only `(slot_id, slot_version, data_hash)`, never `smart_contract_id`: [3](#0-2) 

This mirrors the reported Opera-Bridge flaw: the message hash omits the equivalent of "chain ID + processing contract address," so a signature valid for one deployment/contract context is equally valid in another context that shares the same signer/slot-id namespace. In this codebase, slot assignment is a simple ordinal position derived from each StackerDB contract's `stackerdb-get-signer-slots` output, so the same address is frequently assigned the same `slot_id` across different StackerDB contracts (e.g., across `.signers-1-1`, `.signers-2-1`, etc., or any other StackerDB contract deployed with the same/overlapping signer set ordering): [4](#0-3) 

Given a chunk `(slot_id, slot_version, sig, data)` that was legitimately posted and accepted in StackerDB contract A (visible on the public unauthenticated `/v2/stackerdb/.../chunks` POST endpoint, or via p2p `StackerDBPushChunk` gossip), an attacker can resubmit the identical bytes to StackerDB contract B. If contract B assigns the same signer address to the same `slot_id`, and the replicated chunk's `slot_version` is `>=` B's currently-stored version for that slot, `validate_received_chunk` accepts it — because the signature check only verifies `(slot_id, slot_version, data_hash)`, none of which differ between A and B for the identical replayed bytes.

### Impact Explanation
This allows an unauthenticated remote actor to write forged/misattributed data into a StackerDB contract that the legitimate signer never intended to submit there, and that write is then propagated to the wider network as a legitimately-signed chunk (StackerDB inventories/pushes treat it as valid, and it can be gossiped to all replica peers). This is an unauthorized write to state / network-wide propagation of forged data — messages intended for one signer-message channel (e.g., one reward cycle's signer set or one purpose-specific contract) can be spoofed into another, undermining any downstream logic that trusts "this chunk is attributable strictly to this contract's context."

### Likelihood Explanation
Exploitation only requires: (1) observing one validly-signed, already-published chunk (chunks are gossip data, not secret), and (2) finding a second StackerDB contract where the signing address occupies the same slot index with a slot version not newer than the replayed one. Both conditions are common in practice because slot assignment is a simple ordinal enumeration of the signer set, so cross-cycle/cross-contract slot collisions for the same signer are frequent. No node secret or privileged role is needed — the replay can be submitted through the public, unauthenticated chunk-post HTTP endpoint or p2p push-chunk path.

### Recommendation
Incorporate the StackerDB's `smart_contract_id` (and ideally a network/chain discriminator) into `SlotMetadata::auth_digest()`, e.g. `hash(contract_id || slot_id || slot_version || data_hash)`, and update `sign`/`verify`/`StackerDBChunkData` accordingly so that a signature is only valid for the specific StackerDB it was created for. This requires a version bump / migration path since it changes the signed payload format.

### Proof of Concept
1. Deploy two StackerDB contracts, A and B, where signer `S` is assigned `slot_id = 0` in both (this occurs naturally whenever ordinal signer-set positions coincide across contracts/cycles, as computed by `eval_signer_slots`).
2. `S` signs and posts a chunk `(slot_id=0, slot_version=5, data=D)` to contract A via the public chunk POST endpoint; it is accepted (verified against `auth_digest = hash(0 || 5 || sha512_256(D))`).
3. An attacker captures this exact wire payload (`StackerDBChunkData { slot_id: 0, slot_version: 5, sig, data: D }`) from public gossip/API and POSTs it unmodified to contract B's chunk endpoint.
4. `validate_received_chunk` for contract B looks up `S` as the owner of slot 0, calls `slot_metadata.verify(&S)`, which recomputes the identical digest `hash(0 || 5 || sha512_256(D))` and succeeds — the chunk is accepted into B even though `S` never signed anything referencing contract B.

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

**File:** libstackerdb/src/libstackerdb.rs (L168-193)
```rust
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

**File:** stackslib/src/net/stackerdb/config.rs (L205-243)
```rust
    fn eval_signer_slots(
        chainstate: &mut StacksChainState,
        burn_dbconn: &dyn BurnStateDB,
        contract_id: &QualifiedContractIdentifier,
        tip: &StacksBlockId,
    ) -> Result<Vec<(StacksAddress, u32)>, NetError> {
        let value = chainstate.eval_read_only(
            burn_dbconn,
            tip,
            contract_id,
            &format!("({STACKERDB_SLOTS_FUNCTION})"),
        )?;

        let result = value.expect_result()?;
        let slot_list = match result {
            Err(err_val) => {
                let err_code = err_val.expect_u128()?;
                let reason = format!(
                    "Contract {} failed to run `stackerdb-get-signer-slots`: error u{}",
                    contract_id, &err_code
                );
                warn!("{}", &reason);
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }
            Ok(ok_val) => ok_val.expect_list()?,
        };

        let mut total_num_slots = 0u32;
        let mut ret = vec![];
        for slot_value in slot_list.into_iter() {
            let (addr, num_slots) =
                Self::parse_slot_entry(slot_value, contract_id).map_err(|e| {
                    warn!("Failed to parse StackerDB slot entry: {}", &e);
                    NetError::InvalidStackerDBContract(contract_id.clone(), e)
                })?;

```

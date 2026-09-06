### Title
Cross-contract StackerDB chunk signature replay due to missing contract identity binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` binds a chunk signature only to `slot_id`, `slot_version`, and `data_hash`, never to the StackerDB contract's `QualifiedContractIdentifier`. Because `StackerDBTx::try_replace_chunk` verifies the signature against the *target* contract's stored signer for that slot rather than the contract the chunk was originally signed for, a legitimately signed chunk from contract A can be replayed and accepted into a different contract B whenever the same address happens to own the same `slot_id` there.

### Finding Description
The signature payload is computed in `SlotMetadata::auth_digest`: [1](#0-0) 
and used unchanged by both `sign` and `verify`: [2](#0-1) 

No contract identifier is included in that digest. The write-acceptance path, `StackerDBTx::try_replace_chunk`, looks up the *target* contract's own recorded signer/version for `slot_desc.slot_id` and calls `slot_desc.verify(&slot_validation.signer)`: [3](#0-2) 

`verify` only checks that the recovered pubkey hash matches `slot_validation.signer` (the address configured for that slot **in the target contract**) — it performs no check that the chunk was actually intended for that contract. Since the same signer address can legitimately be configured for slot N in multiple StackerDB contracts (e.g., the same signer set is reused across `signers-0-0`, `signers-0-1`, etc., or an attacker-chosen contract), a chunk signed once for contract A's slot 3 is a bit-for-bit valid, verifiable `StackerDBChunkData`/`SlotMetadata` for contract B's slot 3 as well, as long as `slot_desc.slot_version` exceeds contract B's independently tracked lamport version for that slot (checked via `slot_desc.slot_version <= slot_validation.version` and `slot_desc.slot_version > self.config.max_writes`). Because the attacker fully controls `slot_version` at signing time, they can simply pick a version high enough to be fresh in the target contract too, subject only to `max_writes`.

The equality the code implicitly assumes — "a chunk signature valid for contract A implies authorization to write into contract A only" — is broken: the signature is authenticated to `(slot_id, slot_version, data_hash)` **without regard to contract**, so it is equally valid input to `try_replace_chunk`/`validate_received_chunk` for any other contract where the recovered address owns the same slot.

### Impact Explanation
An attacker who legitimately owns a slot in one StackerDB contract can forge acceptance of arbitrary attacker-chosen data into a different StackerDB contract that shares the same slot owner, via the RPC POST endpoint (`/v2/stackerdb/<addr>/<name>/chunks`, handled ultimately by `poststackerdbchunk.rs` → `try_replace_chunk`) or via P2P gossip of a `StackerDBPushChunk` message reaching `validate_received_chunk`/`try_replace_chunk`. This is an unauthenticated-for-that-contract write to StackerDB state that other nodes will store and gossip further, i.e., network-wide propagation of a forged chunk into a StackerDB context it was never authorized for. This matches the Critical category "unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data."

### Likelihood Explanation
Preconditions: the attacker must legitimately be a signer for slot N in at least one StackerDB contract, and there must exist a second StackerDB contract (either another legitimate one, e.g. sibling `signers-*` contracts sharing the same signer set across reward cycles, or one the attacker can influence/deploy) where the same address is also assigned slot N. Given the Stacks signer set is largely stable/reused across reward-cycle-specific StackerDB contracts, this precondition is realistically satisfiable without any privileged access. The attacker only needs to sign one chunk locally with their own key (no secrets from others required) and submit it over the already-reachable RPC or P2P port; the action is repeatable for every version increment up to `max_writes`.

### Recommendation
Include the StackerDB contract's `QualifiedContractIdentifier` (or a canonical contract identifier hash) in `SlotMetadata::auth_digest()` so that signatures are bound to a specific contract and cannot be replayed across contracts, even when slot ownership overlaps. This requires updating `sign`/`verify`/`auth_digest` signatures to take the contract ID, and updating all call sites (`StackerDBChunkData::sign`, `try_replace_chunk`, `validate_received_chunk`, RPC/sync code) accordingly.

### Proof of Concept
Rust test plan in `stackslib::net::stackerdb::tests::db` (or a new test alongside existing `try_replace_chunk` tests):
1. Create two `StackerDBs`/`StackerDBTx` test databases (or two contracts in the same DB), `contract_a` and `contract_b`, each configured via `create_stackerdb` with the same `StacksAddress` owning `slot_id = 3` in both.
2. Sign a `StackerDBChunkData` for `slot_id = 3`, `slot_version = 1`, with arbitrary attacker-chosen `data`, using that address's private key (`StackerDBChunkData::sign`).
3. Call `tx.try_replace_chunk(&contract_a_id, &chunk.get_slot_metadata(), &chunk.data)` — assert it succeeds (legitimate write).
4. Without re-signing, call `tx.try_replace_chunk(&contract_b_id, &chunk.get_slot_metadata(), &chunk.data)` — assert it **also succeeds**, demonstrating that a chunk authenticated for contract A is accepted and stored under contract B, i.e. `slot_desc.verify(&slot_validation.signer)` returns `Ok(true)` in both contexts despite `smart_contract` differing.
5. Assertion site: the success of the second `try_replace_chunk` call at `stackslib/src/net/stackerdb/db.rs` lines 400-437 (specifically passing the `verify` check at line 418) is the proof that contract identity is not enforced.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L160-166)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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
```

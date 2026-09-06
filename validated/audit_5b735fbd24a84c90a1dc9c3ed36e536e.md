Confirmed: the signature digest that authenticates a StackerDB chunk (`SlotMetadata::auth_digest`) commits only to `slot_id`, `slot_version`, and `data_hash` — it never includes the smart contract identifier. `try_replace_chunk()` looks up the expected signer for `(smart_contract, slot_id)` and calls `slot_desc.verify(&slot_validation.signer)`, which only checks that the signature recovers to that signer's address; it never checks that the signature was produced *for this specific contract's replica*.

### Title
StackerDB chunk signatures are not bound to the target contract, enabling cross-database chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
A signer's chunk signature authenticates `(slot_id, slot_version, data_hash)` only, with no binding to the `QualifiedContractIdentifier` of the StackerDB it was intended for. Any signer who owns the same `slot_id` in two different StackerDB replicas (a common configuration — e.g. successive reward-cycle signer-set contracts, or multiple StackerDB contracts sharing the signer set) can have a chunk signed for replica A accepted as valid for replica B, since verification only checks the recovered address against the per-slot signer of the *target* replica, never the contract it was actually authorized/signed against.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) . Signing and verification operate purely on this digest: [2](#0-1) .

`StackerDBTx::try_replace_chunk` (the write path used both for local `postStackerDBChunk` RPC writes and for chunks received from the network) fetches the expected signer *for the target smart_contract's slot* and verifies the presented `SlotMetadata` against that signer address only: [3](#0-2) . Likewise, the network sync/gossip validation path `PeerNetwork::validate_received_chunk` fetches the per-contract slot signer and calls `slot_metadata.verify(&addr)` with no contract binding: [4](#0-3) .

Because the signed digest never encodes *which* StackerDB contract the chunk belongs to, this reproduces the reported bug class: a signature/approval created in one context (contract A, slot N) is valid in a second, unintended context (contract B, slot N) whenever the same principal is assigned the same `slot_id` in both — exactly analogous to `keeperApprovedFor` being shared across `closeLoan()`/`forecloseLoan()` contexts in the original report. Here the "equality" being broken is: *signed-for-contract-A* ≠ *authenticated-for-contract-B*, but the code treats them as equal.

### Impact Explanation
This allows unauthorized/forged-context writes: an old, validly-signed chunk from contract A can be relayed or resubmitted and accepted as authentic content in contract B's identically-slotted position, as long as the `slot_version`/freshness/staleness checks in the target replica happen to permit it (freshness is only checked against B's own version counter — [5](#0-4)  — not against provenance). This is a "network-wide propagation of forged data" style issue per the impact categories, since the chunk is genuinely signed by the correct address but for the wrong replica, and gossip relayers have no way to detect or reject the mismatch since the digest itself carries no contract binding.

### Likelihood Explanation
Requires: (1) a principal assigned the same `slot_id` across two different StackerDB contracts (a realistic operational pattern, e.g. rotating reward-cycle StackerDB contracts reuse the same signer set and slot ordering), and (2) an attacker or the signer themselves reusing/replaying a previously-signed chunk into the other contract. No secret key or admin role is needed — anyone observing gossip traffic (or the signer, intentionally or accidentally via tooling reuse) can replay chunk bytes+signature pairs to a different contract's `postStackerDBChunk` endpoint or during sync.

### Recommendation
Include the `QualifiedContractIdentifier` (or a stable per-DB identifier) in `SlotMetadata::auth_digest()` so a chunk signature is cryptographically bound to the specific StackerDB replica it was produced for, mirroring the report's recommendation of separating approval scopes so one authorization context cannot be reused in another.

### Proof of Concept
1. Configure two StackerDB contracts, A and B, each assigning `slot_id = 0` to the same signer address `S` (a realistic case when the same signer set backs multiple contracts).
2. `S` signs a chunk `(slot_id=0, slot_version=5, data_hash=H)` for contract A via `StackerDBChunkData::sign` and submits it to contract A, which accepts it (`try_replace_chunk` succeeds using `libstackerdb/src/libstackerdb.rs:226-231` and `stackslib/src/net/stackerdb/db.rs:400-438`).
3. An observer captures this `(SlotMetadata, chunk bytes)` pair from gossip/RPC.
4. The observer resubmits the identical `(SlotMetadata, chunk bytes)` to contract B's slot 0 (assuming B's current version for slot 0 is `< 5`). `try_replace_chunk` for B looks up B's own slot-0 signer (`S`), calls `slot_desc.verify(&S)` which succeeds (same digest, same signer), passes the staleness/max-writes checks against B's own counters, and stores the chunk as authentic content in B — despite it never having been signed "for B".

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

**File:** stackslib/src/net/stackerdb/db.rs (L424-436)
```rust
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

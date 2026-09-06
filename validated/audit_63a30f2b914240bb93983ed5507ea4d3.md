### Title
`SlotMetadata::auth_digest` omits the StackerDB contract identifier, enabling cross-contract chunk replay - (`libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` computes the signed digest as `SHA512/256(slot_id || slot_version || data_hash)` with no binding to the target StackerDB `smart_contract` identifier. Since `StackerDBTx::try_replace_chunk` verifies chunks against the signer address associated with `(smart_contract, slot_id)` in its own local table, a single valid `StackerDBChunkData` signed by an address that owns slot `N` in contract A will also validate against contract B's slot `N` if that same address is registered as the owner of slot `N` there, letting the message be replayed/written into a StackerDB it was never intended for.

### Finding Description
`SlotMetadata::auth_digest` at `libstackerdb/src/libstackerdb.rs:159-166` is:
```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
``` [1](#0-0) 
This digest is what `sign()` and `verify()` operate over, and `StackerDBChunkData::recover_pk`/`verify` derive `SlotMetadata` from the chunk and reuse the same digest [2](#0-1) [3](#0-2) . No `QualifiedContractIdentifier` byte is ever mixed into the hash.

On the write path, `StackerDBTx::try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs:400-438` looks up the expected signer for `(smart_contract, slot_desc.slot_id)` via `get_slot_validation`, then checks `slot_desc.verify(&slot_validation.signer)`:
```rust
let slot_validation = self
    .get_slot_validation(smart_contract, slot_desc.slot_id)?
    .ok_or(net_error::NoSuchSlot(...))?;
if !slot_desc.verify(&slot_validation.signer)? {
    return Err(net_error::BadSlotSigner(...));
}
``` [4](#0-3) 
Because `smart_contract` is only used to look up the *expected signer address* for that contract's own table, and is never part of the cryptographic material that was actually signed, the `smart_contract` parameter is not authenticated at all — it's purely a local index key. If the same address `X` is configured as the owner of slot `N` in both contract A and contract B (e.g., the same signer/operator address appears in two different `stacker_db_configs` entries, which is a normal, legitimate multi-DB deployment scenario, not an attack on the contract config itself), then a chunk `{slot_id: N, slot_version: v, sig, data}` that `X` validly signed for contract A will pass `slot_desc.verify(&slot_validation.signer)` against contract B's table too, since `slot_validation.signer` resolves to the same address `X` and the signature digest contains no contract-scoping bytes.

### Impact Explanation
An attacker (or anyone relaying gossip/RPC POST chunk traffic) can take a chunk validly signed once for StackerDB contract A's slot `N` and successfully write/replay it into StackerDB contract B's slot `N`, provided (a) the same address is the registered slot-`N` signer in both contracts and (b) the supplied `slot_version` exceeds contract B's currently stored version for that slot and is within `max_writes`. This is an unauthenticated/unauthorized write of attacker-chosen prior content into a StackerDB instance it was never signed for, and that content is subsequently gossiped/served to peers as canonical StackerDB state for contract B — matching the Critical category "unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data." The write is repeatable for every reachable contract pair sharing an address/slot-index collision.

### Likelihood Explanation
Preconditions: two StackerDB contracts configured on the node (`stacker_db_configs`) that both assign the same slot index to the same `StacksAddress` (plausible for the same signer/operator appearing across multiple reward-cycle or purpose-specific StackerDB configs). No secret, RPC token, or privileged role is needed — any party that can observe or replay a previously broadcast/posted `StackerDBChunkData` (via P2P gossip or the `/v2/stackerdb/.../chunks` POST endpoint) targeting contract B can trigger `try_replace_chunk` for contract B using material signed for contract A. Attacker cost is a single replayed message per target contract.

### Recommendation
Bind the `smart_contract` (`QualifiedContractIdentifier`) into `SlotMetadata::auth_digest`, e.g. hash `smart_contract.to_string()` (or its serialized issuer+name bytes) alongside `slot_id`, `slot_version`, and `data_hash`, and update `sign`/`verify`/`recover_pk` call sites (`StackerDBChunkData::sign`, `verify`, `recover_pk`, and any place computing `get_slot_metadata().auth_digest()`) to pass the contract identifier through so the signature is scoped to (contract, slot_id, slot_version, data_hash).

### Proof of Concept
Rust test in `libstackerdb::tests` (or a `stackslib` integration test exercising `StackerDBTx::try_replace_chunk`):
1. Construct `QualifiedContractIdentifier`s `contract_a` and `contract_b`.
2. Using `StackerDBTx::create_stackerdb`, register slot 3 for the same `StacksAddress` (derived from a test `StacksPrivateKey`) in both `contract_a` and `contract_b`.
3. Build `StackerDBChunkData { slot_id: 3, slot_version: 5, data: b"payload-for-A".to_vec(), .. }`, call `.sign(&privk)` once (signing only binds slot_id/slot_version/data_hash per current `auth_digest`).
4. Call `try_replace_chunk(&contract_a, &chunk.get_slot_metadata(), &chunk.data)` — succeeds (expected).
5. Call `try_replace_chunk(&contract_b, &chunk.get_slot_metadata(), &chunk.data)` — with the current implementation this ALSO succeeds, even though the chunk was never signed with contract B in mind.
6. Assert step 5 returns `Ok(())` — demonstrating cross-contract replay; after applying the fix (contract ID bound into `auth_digest`), step 5 should instead fail with `net_error::BadSlotSigner`.

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

**File:** libstackerdb/src/libstackerdb.rs (L233-244)
```rust
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

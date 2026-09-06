### Title
StackerDB chunk signatures omit the contract identifier, enabling cross-StackerDB replay of validly-signed chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest as `sha512/256(slot_id || slot_version || data_hash)`, with no binding to the StackerDB's smart-contract identifier (or any other DB-scoping value). Any chunk signature is therefore valid across *every* StackerDB replica in which the signing address happens to own the same `slot_id`, not just the DB it was originally produced for.

### Finding Description
The signing/verification routines are: [1](#0-0) 

`auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`. Verification (`slot_metadata.verify(&addr)`) recovers the public key from this digest and checks the resulting `Hash160` against the expected signer address for that slot — but the expected signer address is looked up *externally*, keyed by `(smart_contract_id, slot_id)`, in `validate_received_chunk`: [2](#0-1) 

Because the contract/DB identity never enters the signed transcript, a signature that a signer produced to authenticate slot `k` version `v` data `D` in StackerDB contract `A` is *also* a valid signature for slot `k` version `v` data `D` in any other StackerDB contract `B`, provided the same address is configured as the owner of slot `k` in `B`. This is exactly the missing-parameter class described in the reference report: important context (the "which StackerDB is this for" parameter, analogous to PLONK's missing `[Qci]1` commitments) is left out of the value that is supposed to bind the whole transcript together.

This is reachable both via the unauthenticated HTTP write path (`POST /v2/stackerdb/.../chunks` handled in `stackslib/src/net/api/poststackerdbchunk.rs`, which ultimately calls the same `verify`/`try_replace_chunk` logic) and via unsolicited P2P gossip (`handle_unsolicited_StackerDBPushChunk`, using `validate_received_chunk`): [3](#0-2) 

Signer/miner deployments run multiple concurrent StackerDB contracts with overlapping signer-address membership (e.g. the rotating `signers-0-xxx.clar` / `signers-1-xxx.clar` contracts used for adjacent reward cycles), so the precondition — the same address owning the same slot index in two different contracts at the same time — is realistic in normal node operation, not a contrived edge case.

### Impact Explanation
An attacker who observes (via gossip or a public StackerDB read endpoint) a legitimately-signed chunk for one StackerDB contract can resubmit the exact same wire bytes (`slot_id`, `slot_version`, `sig`, `data`) against a *different* StackerDB contract that assigns the same slot index to the same signer address. `validate_received_chunk`/`SlotMetadata::verify` will accept it as authentic for the second contract even though that signer never authorized data for that DB/context. This lets an unprivileged remote party cause a node to store and further gossip data under the wrong StackerDB context — i.e., non-context-authenticated data is accepted and propagated as if it were properly authenticated for that specific replica, which corresponds to the "authenticated vs. stored" equality break called out in the analog rules (High: serving/propagating data that was not actually committed-to for the target context as if it were canonical for that context).

### Likelihood Explanation
No secret key material is required — the attacker only needs a previously broadcast, validly-signed chunk (chunks are gossiped in the clear and are also retrievable via the read API) and knowledge that the signing address also owns the same slot index in a second, concurrently-configured StackerDB (a condition that naturally occurs with the rotating `signers-*` contracts across reward cycles). The write path itself (`poststackerdbchunk.rs` / unsolicited `StackerDBPushChunk`) requires no authentication beyond the (broken) chunk signature check.

### Recommendation
Include the StackerDB's `smart_contract_id` (and ideally the config-derived parameters such as `chunk_size`/`num_slots` or a domain-separation tag) inside `SlotMetadata::auth_digest()`, so that a signature over `(contract_id, slot_id, slot_version, data_hash)` cannot be replayed against a different contract. This mirrors the report's remedy of ensuring the Fiat-Shamir transcript binds all context-defining parameters, not just a subset.

### Proof of Concept
1. Configure two StackerDB contracts, `A` and `B`, each assigning slot `0` to the same signer address `X` (realistic with rotating `signers-0-xxx`/`signers-1-xxx` contracts, both active concurrently, per `stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar` / `signers-1-xxx.clar`).
2. Signer `X` legitimately signs and publishes chunk `(slot_id=0, slot_version=1, data=D)` for contract `A` (observed on the P2P network or fetched via the public StackerDB chunk-read endpoint).
3. An attacker with no keys resends the identical `StackerDBChunkData { slot_id: 0, slot_version: 1, sig, data: D }` to a node's `POST /v2/stackerdb/.../chunks` endpoint (or as an unsolicited `StackerDBPushChunk`) for contract `B`.
4. `validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:679-697`) looks up `X` as the expected signer for `B`'s slot 0, calls `slot_metadata.verify(&X)`, which succeeds because `auth_digest()` never encodes which contract the signature was for — the chunk is accepted and stored/propagated under contract `B` even though `X` never authorized data for `B`. [4](#0-3) 
This existing test only exercises the *wrong-signer* case (different address); it does not cover the cross-contract replay scenario because the test harness never constructs two StackerDB contracts assigning the same signer to the same slot, which is why the gap is unnoticed by current coverage.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-656)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
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

**File:** stackslib/src/net/stackerdb/tests/db.rs (L429-443)
```rust
        // should fail -- bad signature
        chunk_data.slot_version = 2;
        if let Err(net_error::BadSlotSigner(stacker, slot_id)) =
            tx.try_replace_chunk(&sc, &chunk_data.get_slot_metadata(), &chunk_data.data)
        {
            assert_eq!(stacker, addrs[i]);
            assert_eq!(slot_id, i as u32);
        } else {
            eprintln!(
                "{}",
                tx.try_replace_chunk(&sc, &chunk_data.get_slot_metadata(), &chunk_data.data)
                    .unwrap_err()
            );
            panic!("Did not get BadSlotSigner");
        }
```

### Title
Cross-contract StackerDB chunk signature replay due to missing contract binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` only hashes `slot_id`, `slot_version`, and `data_hash`, never the StackerDB's smart-contract identifier, so the ECDSA signature produced over this digest is valid for *any* StackerDB contract, not just the one the signer intended. Because `PeerNetwork::validate_received_chunk` (stackslib/src/net/stackerdb/mod.rs) verifies the chunk purely against this contract-agnostic digest, a chunk legitimately signed for contract X's slot N can be replayed and accepted into contract Y's slot N whenever the same address is the configured signer of slot N in both replicas.

### Finding Description
The intended equality is: *the bytes signed by the slot owner should equal the bytes verified, scoped to a specific `(smart_contract_id, slot_id, slot_version, data_hash)` tuple.* Instead, `auth_digest` computes: [1](#0-0) 

which omits `smart_contract_id` entirely. `SlotMetadata::sign` and `SlotMetadata::verify` both operate over this same contract-agnostic digest: [2](#0-1) 

`StackerDBChunkData::verify`/`get_slot_metadata` simply forwards to `SlotMetadata::verify` with no additional contract-scoping input: [3](#0-2) 

On the consuming side, `PeerNetwork::validate_received_chunk` resolves the expected signer purely from `(smart_contract_id, slot_id)` via `get_slot_signer`, then checks the signature against that address using the same contract-agnostic digest: [4](#0-3) 

There is no step anywhere in this path that mixes the contract identifier into the signed material. Consequently, if address `A` is configured as the signer of slot 3 in both contract X and contract Y (a routine occurrence in Stacks, since signer sets are frequently re-derived across `.signers-*` reward-cycle contracts and other StackerDB-based applications with overlapping signer rosters), a `StackerDBChunkData` signed for `(X, slot=3, version=5, data=D)` is bit-for-bit indistinguishable, cryptographically, from a valid chunk for `(Y, slot=3, version=5, data=D)`. An attacker (or even an honest third party replaying/mirroring gossip) can submit that same `StackerDBChunkData` message to contract Y's `handle_unsolicited_StackerDBPushChunk`/`validate_received_chunk` path, and it will pass the signature check, the version check, and the size check, and be accepted as canonically written to Y's slot 3 — even though the signer never authorized that specific write for Y.

### Impact Explanation
This allows unauthenticated cross-database forgery/replay: data legitimately written and signed for one StackerDB contract can be injected as validly-signed into a completely different StackerDB contract's slot, as long as the two contracts happen to assign the same signer to the same slot index. Because accepted chunks are stored and then rebroadcast to the node's DB-neighbors, this is a network-propagating forged-write vector — it satisfies the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" Critical categories. It undermines the isolation applications expect between distinct StackerDB replicas (e.g., mixing up signer-message chunks between different reward-cycle `.signers` contracts, or between a signer-messages DB and an unrelated application DB using overlapping addresses).

### Likelihood Explanation
Preconditions: the attacker must control or observe a validly-signed `StackerDBChunkData` for some contract X, slot N, and there must exist a second contract Y where the same address is configured as slot N's owner (a config-dependent but not rare condition, since Stacks signer sets are reused/rotated across many `.signers-*` contracts and other StackerDB apps). No secret, private key, or privileged role is needed by the replaying attacker — they only need to have observed the gossip traffic (which is broadcast p2p data) and re-send it to any node replicating contract Y, which is a completely open, unauthenticated action over the P2P port. This is trivially repeatable per captured chunk.

### Recommendation
Bind the StackerDB contract identifier into the signed digest. Change `SlotMetadata::auth_digest` (and correspondingly `sign`/`verify`) to also hash the `QualifiedContractIdentifier` (or a stable serialization of it) of the target StackerDB, and thread this contract ID through `StackerDBChunkData::sign`/`verify`/`get_slot_metadata` and `validate_received_chunk` so the signature is contract-scoped. This is a wire-format/signing-scheme change and needs a version gate or coordinated rollout across signer software.

### Proof of Concept
Rust test plan (net/stackerdb test module or a new libstackerdb test):
1. Create two `StackerDBConfig`s / two contract IDs `X` and `Y`, each configuring the same `StacksAddress` (derived from key `K`) as owner of slot 3.
2. Build `StackerDBChunkData { slot_id: 3, slot_version: 5, data: D, .. }`, call `.sign(&K)` — this signs `auth_digest()` which only depends on `(3, 5, hash(D))`.
3. Call `validate_received_chunk(&contract_Y_id, &config_Y, &chunk, &expected_versions_for_Y)` and assert it returns `Ok(true)`, even though this `StackerDBChunkData` was never produced/authorized against contract Y (it's the exact same signed bytes originally intended only for contract X).
4. This demonstrates that `slot_metadata.verify(&addr)` at stackslib/src/net/stackerdb/mod.rs:691 succeeds identically for both contracts, confirming the digest is contract-agnostic and the replay succeeds.

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

**File:** libstackerdb/src/libstackerdb.rs (L213-244)
```rust
    /// Create an owned SlotMetadata describing the metadata of this slot.
    pub fn get_slot_metadata(&self) -> SlotMetadata {
        SlotMetadata {
            slot_id: self.slot_id,
            slot_version: self.slot_version,
            data_hash: self.data_hash(),
            signature: self.sig.clone(),
        }
    }

    /// Sign this given chunk data message with the given private key.
    /// Sets self.signature to the signature.
    /// Fails if the underlying signing library fails.
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

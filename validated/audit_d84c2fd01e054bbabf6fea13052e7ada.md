### Title
StackerDB chunk signatures are not bound to the target contract, enabling cross-StackerDB chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the digest that a stacker signs over as `sha512_256(slot_id || slot_version || data_hash)` [1](#0-0) . The `QualifiedContractIdentifier` (i.e. which StackerDB/smart contract the chunk belongs to) is never mixed into this digest. Chunk acceptance (`try_replace_chunk` in the DB layer, and `PeerNetwork::validate_received_chunk` in the P2P layer) authenticates a chunk purely by recovering the signer from this contract‑agnostic digest and comparing it against the slot's configured signer address for *that* contract [2](#0-1) [3](#0-2) . This is structurally the same "audience"-less authentication weakness described in the Hono JWT advisory: the signature proves *who* signed but not *which service/StackerDB* the signature was intended for.

### Finding Description
A stacker/signer key can be, and in Nakamoto commonly is, registered as the valid signer for the same `slot_id` across more than one StackerDB contract in the same reward cycle (the boot contracts are literally named with parallel pairs such as `signers-0-xxx.clar` / `signers-1-xxx.clar`, whose slot assignment is derived from the same reward-set ordering) [4](#0-3) [5](#0-4) .

Because `SlotMetadata::verify()` only checks that the recovered public key hashes to the expected `StacksAddress` for the given `(slot_id, slot_version, data_hash)` triple — and never checks the `smart_contract` identifier the chunk is stored under [6](#0-5)  — a chunk signature that was produced (and legitimately broadcast) for contract A's slot `N` is equally valid input for contract B's slot `N`, as long as:
- the same signer address owns slot `N` in both contracts, and
- version/size/write-count checks in the target contract are satisfied (attacker fully controls `slot_version` and can pick data whose hash matches, or simply replay the exact same `(slot_id, slot_version, data, sig)` bytes as an unsolicited `StackerDBPushChunk` or `POST /v2/stackerdb/.../chunks` against the *other* contract).

Both ingestion paths perform contract-scoped lookups (`get_slot_validation(smart_contract, slot_id)`, `get_slot_signer(smart_contract_id, slot_id)`) but pass the *target* contract's expectations into a verification routine whose cryptographic commitment never mentions the contract at all [7](#0-6) [8](#0-7) . This is the "authenticated vs. stored" equality the task asks to look for: the node checks "is this signed by the right address for this slot" but never checks "was this signature scoped to this StackerDB" — exactly analogous to Hono's JWT verifier checking `iss`/`exp` but never `aud`.

### Impact Explanation
An unprivileged remote peer that observes (or is itself) a legitimate signer replicated across two contracts can cross-post a chunk that was authorized for one StackerDB into another StackerDB it is also a member of, causing the receiving node to store attacker-selected content under that slot as if it were legitimately written for that contract — a form of unauthorized/forged-data propagation across StackerDB namespaces (network-wide once relayed via `StackerDBChunkInv`/gossip). This matches the "High" bucket (serving/propagating non-canonical or mis-scoped state as canonical for a given StackerDB) without needing any secret key beyond one the attacker/legitimate signer already possesses for the *other* contract.

### Likelihood Explanation
Requires: (1) attacker control of, or access to, a signature that a signer produced for one contract/slot, and (2) that same signer/slot pairing also existing in a second contract the attacker (or any relay peer) can write/push to. Reward-cycle signer StackerDB contract pairs with parallel slot layouts make condition (2) plausible without needing insider access — the attacker only needs to capture/replay a chunk that was already broadcast on the network (chunk data and signatures are not secret; they are meant to be gossiped). This lowers the bar significantly versus a fully novel forgery, though it is bounded by needing two contracts with overlapping signer/slot assignment.

### Recommendation
Include the target `QualifiedContractIdentifier` (and ideally reward-cycle/StackerDB identifier) inside `SlotMetadata::auth_digest()` so a signature is cryptographically bound to the specific StackerDB it is intended for, mirroring the recommended `aud` binding in the Hono advisory. This requires a wire-format/versioning change since `auth_digest` is part of the sign/verify contract in `libstackerdb/src/libstackerdb.rs` and is exercised by `StackerDBs::try_replace_chunk` and `PeerNetwork::validate_received_chunk`.

### Proof of Concept
1. Identify a signer address `S` that owns `slot_id = k` in both StackerDB contract `A` and contract `B` (plausible for the two parallel signer-message contracts within a reward cycle).
2. Capture a chunk `(slot_id=k, slot_version=v, sig, data)` that `S` legitimately signed and pushed to contract `A` (visible via normal gossip/`StackerDBPushChunk`/`GET .../chunks`).
3. Submit the identical `(slot_id=k, slot_version=v', sig, data)` (with `v'` chosen ≥ contract B's current version for slot k, and, if a fresh signature is desired, `data` re-hashed to match `data_hash`) to contract `B` via `POST /v2/stackerdb/{B}/chunks` or an unsolicited `StackerDBPushChunk` for contract `B`.
4. Because `SlotMetadata::verify()` in `try_replace_chunk`/`validate_received_chunk` only checks `(slot_id, slot_version, data_hash)` against `S`'s address for contract `B`, and never checks that the signature was scoped to `A`, the write succeeds and gets relayed as legitimate content for contract `B`.

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

**File:** stackslib/src/net/stackerdb/db.rs (L398-423)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
    }
```

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-1)
```text
;; A StackerDB for a specific message type for signer set 0.
```

**File:** stackslib/src/chainstate/stacks/boot/signers-1-xxx.clar (L1-1)
```text
;; A StackerDB for a specific message type for signer set 1.
```

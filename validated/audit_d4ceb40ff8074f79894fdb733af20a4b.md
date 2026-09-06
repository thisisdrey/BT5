### Title
StackerDB chunk signatures omit the target contract from the signed digest, allowing cross-DB replay of validly-signed chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest that is actually signed/verified for every StackerDB chunk write — commits only to `slot_id`, `slot_version`, and `data_hash`. It never commits to the identity of the StackerDB smart contract (`QualifiedContractIdentifier`) the chunk is destined for. Both the local write path (`StackerDBTx::try_replace_chunk`) and the network push-validation path (`PeerNetwork::validate_received_chunk`) look up the expected signer *per contract + slot_id*, but the cryptographic check itself never binds the contract, so a signature that is valid for one StackerDB replica's slot is equally valid for any other StackerDB replica's slot with the same `slot_id`/`slot_version`/`data`, as long as the same address is a registered signer for that slot in the second contract. This is the network-layer analog of the reported bug: "valid evidence for one context is accepted as valid evidence for a different context," letting a party carry over authorization from a small/old/irrelevant trade (StackerDB write) into a different, unrelated one.

### Finding Description
The signed authentication digest is defined as: [1](#0-0) 

It hashes only `slot_id`, `slot_version`, and `data_hash` — no contract/DB identifier is included. `sign()`/`verify()` operate purely on this digest: [2](#0-1) 

On the write path, `StackerDBTx::try_replace_chunk` fetches the expected signer for `(smart_contract, slot_id)` via `get_slot_validation`, then calls `slot_desc.verify(&slot_validation.signer)` — but `verify()` never sees or checks `smart_contract`: [3](#0-2) 

On the network path, `PeerNetwork::validate_received_chunk` does the same thing: it resolves the expected signer for `(smart_contract_id, data.slot_id)` and calls `slot_metadata.verify(&addr)`, again without the contract entering the cryptographic check: [4](#0-3) 

Because the digest is contract-agnostic, if the same signer address occupies slot `N` in two different StackerDB contracts (e.g., two different `.signers-<cycle>-<index>` replicas across reward cycles, or two different application StackerDBs configured with overlapping signer sets), a `StackerDBChunkData` message that was validly signed for contract A's slot `N` at version `V` remains a cryptographically valid, freshly-verifiable message for contract B's slot `N`, provided version/freshness constraints in B are also satisfiable (an attacker fully controls `slot_version` up to `max_writes`, and can pick data/version to match). The relayer/attacker does not need any secret beyond what they already legitimately hold for context A — this exactly mirrors the reported bug, where evidence authenticated for one trade is reused, unmodified, to satisfy the check for a different trade, because the check never encodes which trade the evidence belongs to.

### Impact Explanation
This lets a party who is a valid signer in contract A relay/replay a validly-signed chunk (message) into contract B and have it accepted as authentic for B, i.e. **unauthenticated write of attacker-chosen content into a StackerDB replica it wasn't actually authorized/intended for**, and this chunk is then gossiped network-wide via `StackerDBPushChunk`/inventory sync as though it were legitimately authored for that replica (`make_StackerDBChunksInv_or_Nack`, `handle_unsolicited_StackerDBPushChunk`). Depending on which contracts share slot/signer overlap (e.g., successive `.signers-*` reward-cycle contracts, which is the miner/signer StackerDB naming convention used throughout `nakamoto_node`), this can cause stale or out-of-context signer/miner data to be served and propagated as canonical current-cycle data — a High-severity "serving non-canonical state as canonical" / cross-context forged-data-propagation issue.

### Likelihood Explanation
Exploitability requires the attacker to already be a legitimate signer for some slot in at least one StackerDB contract, and for the same address to hold a same-numbered slot in another contract they wish to target — a configuration that plausibly recurs across reward-cycle-scoped `.signers-N-M` contracts since signer sets and slot layouts are often stable/rotating across cycles. No additional secret material is needed beyond signing capability the attacker already legitimately possesses for the source contract, and the replay message is a normal, protocol-conformant `StackerDBChunkData`/`StackerDBPushChunk`. This makes the likelihood moderate-to-high wherever such slot/signer overlap exists.

### Recommendation
Include the destination StackerDB `QualifiedContractIdentifier` (or an equivalent contract identifier) inside `SlotMetadata::auth_digest()` so that the signed digest is scoped uniquely to the specific StackerDB replica. Update `sign()`/`verify()` call sites (`StackerDBChunkData::sign`, `try_replace_chunk`, `validate_received_chunk`, and all test vectors) to pass the contract context into the digest computation, mirroring the report's recommendation to separate evidence records per distinct trade/context.

### Proof of Concept
1. Signer `S` is a registered signer for slot `0` in StackerDB contract A (`.signers-0-0`) and also for slot `0` in contract B (`.signers-1-0`), a common layout across reward cycles.
2. `S` builds a `StackerDBChunkData { slot_id: 0, slot_version: V, data: D }`, computes `auth_digest()` (which only covers `slot_id`, `V`, `hash(D)`), and signs it — a fully legitimate write intended for contract A.
3. An attacker (or `S` acting maliciously) resubmits the identical signed `StackerDBChunkData` as a `StackerDBPushChunk` targeting contract B's slot `0` (with a `slot_version` satisfying B's freshness/`max_writes` constraints).
4. `PeerNetwork::validate_received_chunk` resolves B's expected signer for slot `0` (also `S`), calls `slot_metadata.verify(&addr)`, which succeeds because the digest never encoded which contract it was signed for — the forged-context chunk is accepted, stored via `try_replace_chunk`, and gossiped to the network as canonical content for contract B.

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

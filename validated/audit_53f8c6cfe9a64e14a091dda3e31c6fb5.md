### Title
Cross-contract StackerDB chunk signature replay — signed digest omits contract identity, allowing a valid chunk for one StackerDB to be replayed/stored into another - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures are computed over `(slot_id, slot_version, data_hash)` only. The StackerDB smart-contract identity is never part of the signed digest. Any StackerDB contract that happens to assign the same signer address to the same `slot_id` will accept a chunk that was validly signed for a *different* contract, as long as the version counter is higher than the local one. This is the same root-cause class as the external report: a signature meant to authorize one specific action/context is structurally indistinguishable from — and thus reusable in — a different context that shares the same signed fields.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`verify()` recovers the pubkey from that digest and checks it hashes to the expected principal — again with no contract binding: [2](#0-1) 

When a chunk is written, `StackerDBTx::try_replace_chunk` looks up the *local* per-contract slot owner (`slot_validation.signer`) for the target contract and slot, then simply calls `slot_desc.verify(&slot_validation.signer)`; the contract identifier is used only to select which local table row to check, never fed into the signed material itself: [3](#0-2) 

The same pattern exists on the gossip/validation path, where `StackerDBSync::validate_received_chunk` resolves the expected signer purely from `smart_contract_id` + `slot_id` via `get_slot_signer`, then calls `slot_metadata.verify(&addr)` — again the contract id never enters the cryptographic check: [4](#0-3) 

Consequently, if the same signer address is assigned the same `slot_id` in two different StackerDB contracts that a node tracks (e.g. sibling `.signers-<cycle>-<msg-id>` contracts, or the same slot index reused across reward cycles), a `StackerDBChunkData` signed for contract A is a fully valid, verifiable payload for contract B's identical slot — nothing in the cryptographic material ties it to A. The HTTP `POST /v2/stackerdb/:contract/chunks` endpoint and the P2P `StackerDBPushChunk`/`StackerDBChunkInv` gossip paths both take the contract id from the request/message envelope, separate from the signed chunk fields, so an attacker (any remote, unprivileged peer that has observed the original legitimate broadcast) can resubmit the exact same signed chunk against a different contract path/DB that the target node also serves, and it will pass signature verification there.

This precisely mirrors the reported bug class: `refinanceFull`/`addNewTranche` shared the same `RenegotiationOffer` signature scheme with no field distinguishing which action it authorized, so a signature meant for one operation was replayed into a different, unintended one. Here, the StackerDB chunk-signing scheme has no field distinguishing which StackerDB (contract) the signature authorizes, so a signature meant for one StackerDB replica can be replayed into a different StackerDB replica.

### Impact Explanation
Where slot/version overlap exists across contracts, this allows an unprivileged remote actor to force propagation of validly-signed-but-wrong-context data into a StackerDB instance it wasn't intended for, overwriting/poisoning that slot's canonical content for every node that later syncs or accepts the pushed chunk (`insert_chunk`/`try_replace_chunk` will accept it as an authentic write from the recorded slot owner, and neighbors receiving it via `StackerDBChunkInv`/`StackerDBPushChunk` will further relay it). This is a "forged data propagates network-wide as if canonical for this DB" scenario — the receiving DB has no way to tell the chunk wasn't actually authorized for it, since contract identity is outside the signed material.

### Likelihood Explanation
Exploitation requires: (1) observing/capturing one legitimate, validly-signed `StackerDBChunkData` (trivial — chunks are broadcast in the clear over P2P and are also directly fetchable/servable), and (2) a target contract+slot pair where the same signer address currently owns that same `slot_id` with a lower stored version. Given that StackerDB slot assignment for `.signers-*` boot contracts is deterministic per signer-set ordering and multiple sibling contracts (different message lanes, and potentially repeating reward-cycle indices) commonly reuse the same slot index for the same address, this condition is realistically reachable without any privileged access, and requires no attacker-controlled signing key — only replay of intercepted traffic.

### Recommendation
Bind the StackerDB contract identifier (`QualifiedContractIdentifier`) into the signed digest in `SlotMetadata::auth_digest()` (and therefore into `StackerDBChunkData::sign`/`verify`/`recover_pk`), so a signature is only valid for the specific contract it was produced for. This requires a wire-format/versioning change (or a v2 signing scheme) since it affects `StackerDBChunkData`'s consensus-serialized layout and cross-node compatibility.

### Proof of Concept
1. Node N serves two StackerDB contracts, `contract-A` and `contract-B`, both configured (via their respective boot-contract signer lists) with the same signer address `S` at `slot_id = k`, with `contract-B`'s current stored version for slot `k` lower than some value `v`.
2. Signer `S` legitimately signs and pushes `StackerDBChunkData{slot_id:k, slot_version:v, sig, data}` to `contract-A` (e.g., via normal signer flow, `stackslib/src/net/api/poststackerdbchunk.rs` handling `POST /v2/stackerdb/contract-A/chunks`).
3. An unprivileged attacker observes this chunk on the wire (P2P `StackerDBPushChunk` gossip or by fetching it back from any replica), and resubmits the identical `StackerDBChunkData` bytes to `POST /v2/stackerdb/contract-B/chunks` (or crafts a `StackerDBPushChunk` message addressed to `contract-B`).
4. `try_replace_chunk` for `contract-B` looks up its local `slot_validation.signer` for slot `k` (which is `S`), calls `slot_desc.verify(&S)` — which succeeds because the digest never referenced `contract-A` — and, since `v` exceeds `contract-B`'s stored version, the write is accepted per `stackslib/src/net/stackerdb/db.rs:398-438`, and the forged-context chunk is now stored as canonical data for `contract-B` and eligible for further gossip via `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs:649-717`.

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

**File:** stackslib/src/net/stackerdb/db.rs (L398-438)
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

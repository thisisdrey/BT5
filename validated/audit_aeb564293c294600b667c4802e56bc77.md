## Finding

The StackerDB chunk-authentication scheme signs a message digest that never binds to *which* StackerDB (smart contract) the chunk belongs to. This is the same class of bug as the ERC865 report: the signed payload's "identifier" doesn't uniquely commit to the context it's meant to authenticate, so a valid signature produced for one context can be replayed as valid in a different context.

### Title
StackerDB chunk signatures omit the contract identifier, enabling cross-StackerDB chunk replay - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash`. It never includes the StackerDB's `smart_contract_id`. Consequently, if the same signer address occupies the same `slot_id` in two different StackerDB instances (a very common real-world configuration, e.g. consecutive `.signers-1-N` reward-cycle contracts that reuse the same signer set/ordering), a chunk validly signed and broadcast for StackerDB A can be relayed and accepted as valid for StackerDB B by any unprivileged network peer, without any interaction with A's or B's actual owner for that write.

### Finding Description
`SlotMetadata::auth_digest` [1](#0-0)  hashes only `slot_id`, `slot_version`, and `data_hash`. `sign`/`verify` operate purely on this digest [2](#0-1) , and `StackerDBChunkData::verify` simply forwards to it [3](#0-2) .

Both the write path (`StackerDBTx::try_replace_chunk`) and the gossip/push validation path (`StackerDBSync::validate_received_chunk`) look up the expected signer *per contract* via `get_slot_validation`/`get_slot_signer`, but they only check whether the *signature verifies against that address* — not whether the signature was produced for *this* contract:
- `try_replace_chunk` calls `slot_desc.verify(&slot_validation.signer)` [4](#0-3) .
- `validate_received_chunk` fetches `addr` from `get_slot_signer(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)` [5](#0-4) .

Because the digest never encodes `smart_contract_id`, a signature that is valid for slot `k` in contract A is *also* valid for slot `k` in contract B whenever the address controlling slot `k` is the same in both A and B. This equality break — "signed for A" vs. "accepted as authenticated for B" — is structurally identical to the ERC865 bug: an insufficiently-scoped hash/identifier lets a payload meant for one context be reinterpreted as authentic in another.

### Impact Explanation
This allows network-wide propagation of forged/misattributed data into a StackerDB replica: an unprivileged peer who has observed (or can induce) a valid chunk write in StackerDB A can rebroadcast the identical `StackerDBChunkData`/`StackerDBPushChunkData` (just swapping the target `contract_id` in the wrapping message) to get it accepted into StackerDB B, as long as B's slot `k` owner matches A's slot `k` owner — a condition that commonly holds for signer-set StackerDB contracts across reward cycles when the signer set doesn't change. This can corrupt or desynchronize the target StackerDB's state (e.g., causing a signer's slot in a *newer* cycle's contract to reflect stale/wrong-cycle data), and the forged chunk will legitimately propagate further via the normal StackerDB gossip mechanism, since it passes `validate_received_chunk`.

### Likelihood Explanation
Exploitability depends on the specific deployment condition that the same signer address holds the same `slot_id` across two StackerDB contract instances — which is plausible in practice (unchanged signer sets across consecutive reward cycles, or other StackerDB pairs with overlapping/aligned slot ownership), but is not guaranteed for every contract pair. It requires no secret key, no privileged role, and only the ability to relay/replay previously-seen network traffic, making it a real, low-effort attack vector in the conditions where it applies.

### Recommendation
Include the target StackerDB's `smart_contract_id` (and ideally the current `rc_consensus_hash`/reward-cycle epoch context, matching how `StackerDBPushChunkData` already carries an `rc_consensus_hash`) in `SlotMetadata::auth_digest`, so a chunk signature is only valid for the specific StackerDB it was created for. This is a breaking change to the wire format/signing scheme and must be versioned/rolled out carefully across the network.

### Proof of Concept
1. Configure two StackerDB contracts A and B (e.g., `.signers-1-N` and `.signers-1-N+1`) where slot 0 is owned by the same signer address `S`.
2. `S` signs and broadcasts a `StackerDBChunkData` for slot 0/version 1 under contract A: `chunk.sign(&S_privkey)`, producing `sig` over `auth_digest(slot_id=0, slot_version=1, data_hash)`.
3. An unprivileged network observer captures this chunk and its signature, then relays it in a new `StackerDBPushChunkData` with `contract_id` set to B (keeping the same `slot_id`, `slot_version`, `sig`, `data`).
4. Because `SlotMetadata::auth_digest` and `verify` never reference `contract_id`, `validate_received_chunk` (via `get_slot_signer(B, 0) == S`) succeeds, and the chunk is accepted/stored into B's slot 0 even though `S` never authorized this write for B.

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

**File:** libstackerdb/src/libstackerdb.rs (L239-244)
```rust
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

## Title
StackerDB chunk signatures lack contract/session binding, enabling cross-context signature replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The Schnorr-proof replay class described in the report (CVE-2022-47930) stems from a signed authentication artifact that omits a session/context identifier from the signed digest, letting a valid signature be replayed outside the context it was produced for. The `SlotMetadata`/`StackerDBChunkData` signing scheme in this repo has the analogous property: the digest that is signed to authenticate a StackerDB chunk write binds only to `slot_id`, `slot_version`, and `data_hash` — never to the `QualifiedContractIdentifier` (the StackerDB "session"/DB instance) it is destined for, nor to any network/chain identifier.

### Finding Description
`SlotMetadata::auth_digest()` computes the signed hash as `SHA512/256(slot_id || slot_version || data_hash)`, with no inclusion of the StackerDB contract ID: [1](#0-0) 

`sign()`/`verify()` operate purely over this digest and a `StacksAddress` (the expected signer for that slot), again with no contract/session context: [2](#0-1) 

Both the direct-write path and the gossip/push path validate a chunk purely by checking (a) size, (b) signer address returned by `get_slot_signer(contract_id, slot_id)`, (c) `SlotMetadata::verify`, and (d) version freshness — but the signature itself never certifies which `contract_id` it was created for: [3](#0-2) 

Because many signer-set StackerDB contracts exist in parallel (one per reward cycle × message lane, e.g. `signers-<cycle>-<msg-id>`), and the same signer public key/slot index is frequently reused across these different contracts and across reward cycles, a chunk signature that was validly produced and observed on the wire for one `(contract_id_A, slot_id, slot_version)` can be re-submitted (replayed) against a *different* `contract_id_B` that happens to assign the same signer to the same `slot_id`, as long as `slot_version` in B is not lower than B's currently stored version — a condition trivially satisfiable for a freshly-created or low-water-mark StackerDB replica (e.g. a new reward cycle's contract starting from version 0). The equality that is broken is "signed for context A" vs. "accepted as authentic in context B": the verifier only checks byte-for-byte signature validity and target-slot ownership, not which StackerDB session the signature was intended to authenticate.

This matches the report's core defect class: a proof/signature that omits a session identifier from its challenge/digest, making it trivially replayable in another valid context by an unprivileged, remote, capture-and-replay attacker (an eavesdropper on gossip/relay traffic, or any reader of the public StackerDB HTTP GET endpoints).

### Impact Explanation
An attacker who observes a validly-signed `StackerDBChunkData` (via public StackerDB read endpoints or p2p gossip) for one contract can, in principle, resubmit its exact bytes and signature to a different StackerDB contract governed by the same signer-to-slot mapping, causing the receiving node to accept and store attacker-supplied historical data as if freshly written under the new context (an unauthorized write of stale/foreign data into a StackerDB slot). This is a state-integrity violation in the class of "authenticated vs. actually-intended" and can propagate via `handle_unsolicited_StackerDBPushChunk`/gossip to the wider network, per the code shown at: [4](#0-3) 

### Likelihood Explanation
Exploitation requires (1) the target slot signer to be identical across the two StackerDB contracts (a common occurrence given signer-set reuse across reward cycles/message lanes), and (2) the target version constraint to be satisfiable. I could not fully verify within the available context whether slot-to-signer index assignment is guaranteed identical across different `signers-X-Y` contracts/reward cycles in the current codebase (the relevant assignment logic in `chainstate/nakamoto/signer_set.rs` and the `create_stackerdb`/`get_slot_signer` implementation in `stackslib/src/net/stackerdb/db.rs` were only partially inspected before the session ended), so likelihood is assessed as plausible but not fully proven end-to-end.

### Recommendation
Include the destination `QualifiedContractIdentifier` (and ideally a chain/network identifier) inside `SlotMetadata::auth_digest()` so that a chunk signature is cryptographically bound to the specific StackerDB it is intended for, preventing cross-contract/cross-reward-cycle replay of otherwise-valid signatures.

### Proof of Concept
Conceptual (not fully executed due to tool-call exhaustion):
1. Identify two StackerDB contracts (e.g., a past reward cycle's `signers-N-1` and current `signers-M-1`) where signer `pk` is assigned to the same `slot_id`.
2. Capture (or independently produce) a validly signed `StackerDBChunkData{slot_id, slot_version, sig, data}` for `signers-N-1`.
3. Submit the identical `StackerDBChunkData` bytes via `POST /v3/stackerdb/<signers-M-1 contract>/chunk` (or via unsolicited p2p `StackerDBPushChunk`) with `slot_version` set above `signers-M-1`'s current stored version for that slot.
4. Because `validate_received_chunk`/HTTP handler only check size, signer-address-for-slot, signature validity, and version freshness — none of which reference `contract_id` — the replayed chunk is accepted and stored under `signers-M-1`, and may be re-gossiped to peers via `handle_unsolicited_StackerDBPushChunk`.

Confidence caveat: I was unable to fully confirm, within remaining tool budget, that slot-index-to-signer assignment actually collides across contracts in the current signer-set/reward-cycle implementation; this would need to be verified against `stackslib/src/chainstate/nakamoto/signer_set.rs` and `stackslib/src/net/stackerdb/db.rs::get_slot_signer`/`create_stackerdb` before treating this as fully confirmed rather than a strong structural analog.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-706)
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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L767-815)
```rust
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }

                // patch inventory -- we'll accept this chunk
                let Some(slot_version) = data
                    .slot_versions
                    .get_mut(chunk_data.chunk_data.slot_id as usize)
                else {
                    error!(
                        "Chunk not accepted with slot_id {}, which is greater than our slot_versions array {} in {}",
                        chunk_data.chunk_data.slot_id,
                        data.slot_versions.len(),
                        chunk_data.contract_id
                    );
                    return Ok((false, false));
                };
                *slot_version = chunk_data.chunk_data.slot_version;

                // wake up the state machine -- force it to begin a new sync if it's asleep
                if let Some(stackerdb_syncs) = self.stacker_db_syncs.as_mut() {
                    if let Some(stackerdb_sync) = stackerdb_syncs.get_mut(&chunk_data.contract_id) {
                        stackerdb_sync.wakeup();
                    }
                }
            }
```

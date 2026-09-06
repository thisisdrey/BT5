### Title
`StackerDBChunkData`/`SlotMetadata` signatures omit the target contract ID, enabling cross-StackerDB chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` — the digest that a signer signs to authorize a StackerDB chunk write — only commits to `slot_id`, `slot_version`, and `data_hash`. It never commits to the `QualifiedContractIdentifier` (the StackerDB instance) that the chunk is meant for. Both the storage layer (`StackerDBTx::try_replace_chunk`) and the P2P validation layer (`PeerNetwork::validate_received_chunk`) verify a chunk's signature against the signer configured for `(contract_id, slot_id)` in *that specific* StackerDB, but the signature itself carries no cryptographic binding to `contract_id`. Consequently, a valid chunk gossiped/posted for one StackerDB contract can be replayed verbatim into a different StackerDB contract that happens to assign the same signer address to the same slot ID, as long as the target's stored version is behind the replayed version — with no need for the signer's private key.

### Finding Description
The signed digest is computed here: [1](#0-0) 

Verification recovers the pubkey hash purely from this digest and compares it to the expected principal — again with no contract-id binding: [2](#0-1) 

On the write path, `StackerDBTx::try_replace_chunk` only checks the signature against the slot's *locally stored* signer for that contract's DB, plus a monotonic version check — it never checks that the signature was produced with `contract_id` in mind: [3](#0-2) 

On the P2P gossip path, `PeerNetwork::validate_received_chunk` performs the equivalent check using `self.stackerdbs.get_slot_signer(smart_contract_id, data.slot_id)` — again scoped only by the caller-supplied `smart_contract_id`, with the actual chunk signature carrying no such scoping: [4](#0-3) 

`handle_unsolicited_StackerDBPushChunk` takes the `contract_id` straight from the attacker-supplied `StackerDBPushChunkData` and calls the above validator against whatever contract is named there, then patches the inventory and wakes the sync state machine to fetch/store the chunk into that DB's slot: [5](#0-4) 

Because the digest/signature is identical regardless of which StackerDB it is destined for, any unprivileged network peer that observes a legitimately-signed chunk for StackerDB `A` can resubmit that exact `(slot_id, slot_version, sig, data)` tuple, wrapped in a message naming StackerDB `B`, to a node that (a) replicates `B` and (b) happens to assign the same signer address to the same `slot_id` in `B` (a very plausible situation for boot contracts, since `.signers-<cycle>-<n>` StackerDBs are frequently populated by an overlapping/identical set of signer addresses across reward cycles, and `.miners` slot assignment can also overlap with other node-controlled contracts). If `B`'s current slot version is behind the replayed version, the replayed chunk is accepted and stored as if it had been authorized for `B`.

This is directly analogous to the `BetFactory` bug: the equality the protocol relies on ("this signature authorizes slot X *in contract C*") is never actually enforced in the cryptographic commitment (the signed digest), just as `Bet::initialize`'s approval didn't encode which specific pool the caller committed to. The fix pattern is the same: include the missing binding value (there, `tokenToPool[asset]`; here, `contract_id`) in the object that participates in the equality check (there, the CREATE2 salt; here, the signed digest).

### Impact Explanation
This allows an unauthenticated, unprivileged network peer to cause unauthorized writes into a StackerDB replica by replaying data that was never signed for that specific StackerDB contract. Depending on which StackerDB contracts are involved (e.g. Nakamoto signer message StackerDBs across different reward cycles, or `.miners`), this can inject stale/foreign, but validly-signed-looking, data into a StackerDB instance where it will be treated as canonical for that contract's slot, potentially causing higher-level signer/miner software that consumes that StackerDB's contents to misinterpret cross-cycle or cross-context messages as legitimate for the wrong context. This is an unauthorized write to state (StackerDB), matching the Critical bucket ("unauthenticated/unauthorized write to state or StackerDB", "network-wide propagation of forged data").

### Likelihood Explanation
Exploitation requires no secret key material — only observing a legitimately broadcast chunk (StackerDB gossip is unauthenticated/public by design) and resending it addressed to a different, remotely-reachable StackerDB contract that a target node replicates and where slot-to-signer assignment happens to coincide. Given that Nakamoto's signer-set StackerDBs are re-derived per reward cycle from largely the same set of stacking signers, and slot assignment ordering can plausibly coincide across cycles or across `.signers-<cycle>-0` / `.signers-<cycle>-1` message-type StackerDB pairs, the precondition (same signer at same slot_id) is realistically achievable, not merely theoretical.

### Recommendation
Include the target `QualifiedContractIdentifier` (or a stable numeric StackerDB ID) as part of `SlotMetadata::auth_digest`, so that a signature is only valid for the specific StackerDB it was created for:
```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.contract_id.serialize_to_vec()); // new: domain separation
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
This requires plumbing the contract ID into `SlotMetadata`/`StackerDBChunkData` (or at minimum into the sign/verify call sites in `db.rs` and `stackerdb/mod.rs`) and is a breaking wire-format/signing change that must be versioned carefully.

### Proof of Concept
1. Node `N` replicates StackerDB contracts `A` (`.signers-0-1`) and `B` (`.signers-1-1`), where signer address `S` is assigned slot `5` in both (plausible since the same stacker often re-registers across consecutive cycles and slot ordering is derived similarly).
2. Legitimate signer `S` signs and pushes chunk `(slot_id=5, slot_version=7, data=D)` to `A`; this is broadcast over P2P as `StackerDBPushChunkData{contract_id: A, chunk_data}`. Signature `sig = Sign(S, auth_digest(5, 7, hash(D)))`.
3. An unprivileged observer captures this message and resends `StackerDBPushChunkData{contract_id: B, chunk_data: (slot_id=5, slot_version=7, sig, data=D)}` to `N`.
4. `handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk("B", config_B, chunk_data, expected_versions_B)`, which fetches `get_slot_signer(B, 5) == S`, computes the same `auth_digest(5,7,hash(D))`, and `slot_metadata.verify(&S)` succeeds because the digest never encoded which contract it belongs to.
5. If `B`'s slot 5 version is currently `< 7`, the chunk is accepted into `B`'s inventory/sync pipeline and eventually stored via `try_replace_chunk`, which performs the identical contract-agnostic check — completing the cross-contract replay without the attacker ever possessing `S`'s private key.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-792)
```rust
    pub fn handle_unsolicited_StackerDBPushChunk(
        &mut self,
        chainstate: &mut StacksChainState,
        event_id: usize,
        preamble: &Preamble,
        chunk_data: &StackerDBPushChunkData,
        send_reply: bool,
    ) -> Result<(bool, bool), net_error> {
        let Some(naddr) = self
            .get_p2p_convo(event_id)
            .map(|convo| convo.to_neighbor_address())
        else {
            debug!(
                "Drop unsolicited StackerDBPushChunk: event ID {} is not connected",
                event_id
            );
            return Ok((false, false));
        };

        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
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
```

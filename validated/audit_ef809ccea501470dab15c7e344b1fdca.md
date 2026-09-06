## Analysis: Cross-contract StackerDB chunk signature replay

I could map this bug class onto the StackerDB chunk-signing scheme in this repo, and it reproduces the same root cause: the signature does not bind to the specific "contract" (StackerDB instance) it is meant to authenticate for.

### Root cause

`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash` — it never includes the StackerDB's `smart_contract_id`: [1](#0-0) 

`sign()`/`verify()` operate on this same digest: [2](#0-1) 

Chunks are validated for storage/relay in `validate_received_chunk`, which looks up the expected slot owner *for the given `smart_contract_id`* and then calls `slot_metadata.verify(&addr)` — again, the contract identity is never part of what's cryptographically checked, only used to look up which address *should* own the slot: [3](#0-2) 

### Why this is exploitable (the "same token ID across collections" analog)

The `.signers-<N>-<M>` boot contracts (one per signer message "lane" M, for signer set N) all derive their slot-to-signer assignment from the *same* per-cycle page (`stackerdb-signer-slots-0`/`-1`), so the same signer address occupies the *same slot index* across every message-lane contract in a given reward cycle: [4](#0-3) [5](#0-4) 

Because `auth_digest` only commits to `(slot_id, slot_version, data_hash)`, a `StackerDBChunkData` legitimately signed by a signer for slot `X` in contract `signers-0-1` (e.g. the BlockResponse lane) is a byte-for-byte valid, independently-verifiable signature for slot `X` in `signers-0-3` (e.g. the Transactions lane) — same signer, same slot index, same signature — the contract identity is never part of the equality being checked. An attacker (or any relaying peer) can therefore replay that `StackerDBChunkData` against a *different* StackerDB contract via `handle_unsolicited_StackerDBPushChunk` or the download-validation path, and `validate_received_chunk` will accept it as authentic for the wrong contract as long as the slot's `version`/`max_writes` constraints are met: [6](#0-5) 

This breaks the same equality as the reported finding: "signed-for-contract-A" is treated as equal to "valid-for-contract-B" purely because the digest omits the contract/domain identifier — directly analogous to `keccak256(abi.encode(_sender, _tokenIds, _rarityWeightIndexes))` omitting `address(this)`.

### Title
Cross-StackerDB-contract chunk signature replay due to missing contract binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` signs only `(slot_id, slot_version, data_hash)`, omitting the target `QualifiedContractIdentifier`. Since the same signer/slot-index mapping is reused across every `.signers-<N>-<M>` message-lane contract in a reward cycle, a chunk validly signed for one contract's slot can be replayed and accepted as authentic for a different contract's identical slot.

### Finding Description
`StackerDBChunkData::sign`/`verify` and `SlotMetadata::sign`/`verify` compute a digest that binds only to `slot_id`, `slot_version`, and the hash of the chunk bytes. `validate_received_chunk` fetches the *expected signer address* using the contract ID (via `get_slot_signer`) but never mixes the contract ID into the cryptographic check itself — it just checks that the recovered/verified address equals the expected one. Because the `.signers-0-*`/`.signers-1-*` boot contracts derive their slot assignments from one shared per-cycle page, an identical `(slot_id, signer)` pairing exists across multiple distinct StackerDB contracts simultaneously.

### Impact Explanation
An attacker who can observe or induce one legitimately-signed chunk (e.g., relayed over the network by any peer) can resubmit the identical `StackerDBChunkData` (slot_id, slot_version, sig, data) against a *different* StackerDB contract that shares the same signer/slot mapping. `validate_received_chunk` will accept it, causing the node to store and further gossip that chunk under the wrong contract, i.e., forged/misattributed data propagated as authentic content for a StackerDB it was never intended for. This is a form of unauthenticated write / forged-data propagation into StackerDB state.

### Likelihood Explanation
Requires the attacker to control or intercept one validly-signed chunk from any of the shared-slot lane contracts (all traffic is unencrypted/observable P2P StackerDB gossip) and know the target contract's expected version/slot constraints — no private keys or privileged roles are required, matching the "remote, unprivileged" analog criteria.

### Recommendation
Include the `smart_contract_id` (or its numeric/domain-separated `stackerdb_id`) as part of `SlotMetadata::auth_digest()`, e.g.:
```rust
fn auth_digest(&self, contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(contract_id.to_string().as_bytes());
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
and thread the contract ID through `sign`/`verify`/`recover_pk` call sites, updating all consumers (`validate_received_chunk`, sync/push paths) accordingly. This is a breaking wire/format change and needs careful backward-compatibility handling across the network.

### Proof of Concept
Conceptually (not runnable without live infra):
1. Signer S is assigned slot 0 in both `.signers-0-1` (BlockResponse lane) and `.signers-0-3` (Transactions lane) for the current reward cycle (confirmed shared assignment in `signers.clar`/`signers_db_get_slots` test).
2. S legitimately signs and pushes a `StackerDBChunkData{slot_id:0, slot_version:5, sig, data}` to `.signers-0-1`.
3. Attacker captures this chunk from network gossip and resends it (unmodified) as a `StackerDBPushChunkData{contract_id: .signers-0-3, chunk_data: <same chunk>}` to a victim node, with `slot_version` satisfying that DB's expected/next version.
4. `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` recovers/validates the signature against `.signers-0-3`'s expected slot-0 signer (same S), which succeeds since the digest never referenced the contract, and the chunk is accepted and stored/relayed as valid data for `.signers-0-3`.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-790)
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
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L12-24)
```text
;; Called internally by the Stacks node.
;; Stores the stackerdb signer slots for a given reward cycle.
;; Since there is one stackerdb per signer message, the `num-slots` field will always be u1.
(define-private (stackerdb-set-signer-slots 
                   (signer-slots (list 4000 { signer: principal, num-slots: uint }))
                   (reward-cycle uint)
                   (set-at-height uint))
	(let ((cycle-mod (mod reward-cycle u2)))
        (map-set cycle-set-height reward-cycle set-at-height)
        (var-set last-set-cycle reward-cycle)
        (if (is-eq cycle-mod u0)
            (ok (var-set stackerdb-signer-slots-0 signer-slots))
            (ok (var-set stackerdb-signer-slots-1 signer-slots)))))
```

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-341)
```rust
    for signer_set in 0..2 {
        for message_id in 0..SIGNER_SLOTS_PER_USER {
            let contract_name =
                ContractName::try_from(format!("signers-{}-{}", &signer_set, &message_id)).unwrap();
            let signers = readonly_call(
                &mut peer,
                &latest_block_id,
                contract_name.clone(),
                ClarityName::from_literal("stackerdb-get-signer-slots"),
                vec![],
            )
            .expect_result_ok()
            .unwrap();

            debug!("Check .{}", contract_name);
            if signer_set == 0 {
                assert_eq!(signers.expect_list().unwrap(), vec![]);
            } else {
                assert_eq!(signers, expected_stackerdb_slots);
            }
        }
    }
```

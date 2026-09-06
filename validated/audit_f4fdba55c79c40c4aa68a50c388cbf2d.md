This confirms the finding. Signer slot assignment is per-reward-cycle, deterministic (sorted by `signing_key`), and stored in two rotating pages (`stackerdb-signer-slots-0`/`-1` in `signers.clar`) that are reused across consecutive reward cycles for the `.signers-{0,1}-<cycle>` StackerDB contracts. Since `SlotMetadata::auth_digest()` binds a signature only to `(slot_id, slot_version, data_hash)` and never to the StackerDB's `QualifiedContractIdentifier`, and slot assignments are frequently stable/identical for a signer across consecutive `.signers-N-<cycle>` contracts, a previously-broadcast, legitimately-signed chunk from one StackerDB contract is a valid signature for the *same* slot in a different StackerDB contract whenever the same address occupies that slot index there too.

<cite repo="ThankGodontt/stacks-core--016" path="libstackerdb/src/libstackerdb.rs" start="159="166" />

### Title
Cross-StackerDB replay of signed chunks due to missing contract-identifier binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest over only `slot_id`, `slot_version`, and `data_hash` [1](#0-0) , and `verify()` recovers the pubkey from that digest and compares its hash against a caller-supplied `principal: &StacksAddress` [2](#0-1) . Nowhere in the signed payload is the target StackerDB's `QualifiedContractIdentifier` (the smart contract governing that particular StackerDB instance) included. All server-side gates — `StackerDBSync::validate_received_chunk` [3](#0-2) , the unsolicited-push handler [4](#0-3) , and the HTTP `POST /v2/stackerdb/.../chunks` handler `StackerDBErrorCodes::BadSigner` check — look up the *expected signer address for that specific contract_id/slot_id* and validate the signature against it, but the signature itself carries no contract binding, so any signature that recovers to the correct address for slot X is accepted for slot X in *any* StackerDB contract where that address is the assigned signer.

### Finding Description
The Stacks signer-set mechanism (`stackslib/src/chainstate/stacks/boot/signers.clar`) assigns StackerDB slot indices deterministically by sorting signers by public key [5](#0-4) , and slot pages are rotated only between even/odd reward cycles (`stackerdb-signer-slots-0`/`stackerdb-signer-slots-1`) [6](#0-5) . This means the same signer very commonly occupies the identical slot index across consecutive `.signers-{0,1}-<cycle>` StackerDB contracts (and potentially across other related StackerDB contracts that share the same signer roster/ordering, e.g. across reward cycles where the signer set is unchanged).

Because `auth_digest()` never mixes in the destination contract identifier, a chunk `(slot_id, slot_version, data_hash, sig)` that a signer legitimately signed and broadcast for StackerDB contract A is *also* a valid, verifiable signature for the same `slot_id` in StackerDB contract B, provided the signer occupies that slot in B as well. An attacker (who needs no private key at all — chunks are broadcast in the clear over the p2p StackerDB gossip protocol and are also queryable) can capture such a chunk from contract A and resubmit it verbatim to contract B's HTTP endpoint or via the p2p `StackerDBPushChunk` message. `validate_received_chunk` only checks the address that `get_slot_signer` returns for `(smart_contract_id, slot_id)` — it does not verify that the signature was produced for *that* smart_contract_id [3](#0-2) . This is structurally the same "equality" violation as the WildFly analog: a security context (here, "authorized to write to contract A's slot") is not properly scoped/reset when the exact same auth artifact is presented in a different security context (contract B's slot).

### Impact Explanation
This allows unauthorized, unsigned-for-that-target writes to a StackerDB replica's state without the actual owner's cooperation, provided a slot/signer coincidence exists across StackerDB instances — an unauthenticated write to state as defined in the "Critical" impact bucket (data is written to a StackerDB slot under an address's identity without that address ever having authorized a write to *that* specific database). This can be leveraged to plant stale/foreign signer-message content into the wrong StackerDB (e.g., replaying a `BlockResponse`/`StateMachineUpdate` payload intended for one reward cycle's contract into another), potentially confusing downstream consumers such as `StackerDBListener` that parse and act on `SignerMessageV0` content by slot/address [7](#0-6) .

### Likelihood Explanation
Exploitation requires no privileged access: chunks are broadcast openly over the network and are directly queryable, and the attacker only needs to identify a `(slot_id)` collision between two StackerDB contracts sharing at least one signer — a condition that is expected to occur routinely given the deterministic, sorted-by-pubkey slot assignment algorithm and the two-page rotation scheme used for consecutive reward cycles.

### Recommendation
Include the target `QualifiedContractIdentifier` (and ideally the reward-cycle/`rc_consensus_hash` context) inside `SlotMetadata::auth_digest()` so that a signature is cryptographically scoped to a single StackerDB instance, then update `sign()`/`verify()` call sites (`libstackerdb/src/libstackerdb.rs`, and callers in `stackslib/src/net/stackerdb/mod.rs`, `stackslib/src/net/api/poststackerdbchunk.rs`) accordingly. This is a wire-format/signing-scheme change requiring coordinated node/signer upgrade.

### Proof of Concept
1. Let signer `S` (address `A`) legitimately own slot `5` in StackerDB contract `.signers-0-100` and also own slot `5` in `.signers-0-102` (plausible given deterministic sort-by-pubkey allocation, unchanged signer roster across cycles).
2. Observe/capture a legitimately-broadcast `StackerDBChunkData { slot_id: 5, slot_version: V, sig: SIG, data: D }` for `.signers-0-100`, e.g. via `StackerDBGetChunk`/`StackerDBChunkData` p2p query.
3. Without possessing `S`'s private key, submit the identical tuple `(slot_id=5, slot_version=V, sig=SIG, data=D)` via `POST /v2/stackerdb/<...>.signers-0-102/chunks` (or via `StackerDBPushChunk` p2p message).
4. `validate_received_chunk`/`SlotMetadata::verify` recovers `SIG` against the auth digest of `(5, V, hash(D))`, which matches address `A` — the expected signer for slot 5 in `.signers-0-102` — so the chunk is accepted and stored in `.signers-0-102` even though `S` never authorized a write to that contract.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L742-767)
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
```

**File:** stackslib/src/chainstate/stacks/boot/mod.rs (L1039-1069)
```rust
        let mut signer_set = BTreeMap::new();
        for entry in entries.iter() {
            let signing_key = entry
                .signer
                .expect("BUG: signing keys should all be set in reward-sets with any signing keys");
            if let Some(existing_entry) = signer_set.get_mut(&signing_key) {
                *existing_entry += entry.amount_stacked;
            } else {
                signer_set.insert(signing_key, entry.amount_stacked);
            };
        }

        let mut signer_set: Vec<_> = signer_set
            .into_iter()
            .filter_map(|(signing_key, stacked_amt)| {
                let weight = u32::try_from(stacked_amt / threshold)
                    .expect("CORRUPTION: Stacker claimed > u32::max() reward slots");
                if weight == 0 {
                    return None;
                }
                Some(NakamotoSignerEntry {
                    signing_key,
                    stacked_amt,
                    weight,
                })
            })
            .collect();

        // finally, we must sort the signer set: the signer participation bit vector depends
        //  on a consensus-critical ordering of the signer set.
        signer_set.sort_by_key(|entry| entry.signing_key);
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L1-24)
```text
(define-data-var last-set-cycle uint u0)
(define-data-var stackerdb-signer-slots-0 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-data-var stackerdb-signer-slots-1 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-map cycle-set-height uint uint)
(define-constant MAX_WRITES u4294967295)
(define-constant CHUNK_SIZE (* u2 u1024 u1024))
(define-constant ERR_NO_SUCH_PAGE u1)
(define-constant ERR_CYCLE_NOT_SET u2)

(define-map cycle-signer-set uint (list 4000 { signer: principal, weight: uint }))

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

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L372-393)
```rust
            for (slot_id, _pk, message) in messages.into_iter() {
                let Some(signer_entry) = &self.signer_entries.get(&slot_id) else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Signer entry not found".into(),
                    ));
                };
                let Ok(signer_pubkey) = StacksPublicKey::from_slice(&signer_entry.signing_key)
                else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Failed to parse signer public key".into(),
                    ));
                };

                match message {
                    SignerMessageV0::BlockResponse(BlockResponse::Accepted(accepted)) => {
                        let BlockAccepted {
                            signer_signature_hash: block_sighash,
                            signature,
                            metadata,
                            response_data,
                        } = accepted;
                        let tenure_extend_timestamp = response_data.tenure_extend_timestamp;
```

### Title
Missing per-contract domain binding in StackerDB chunk signatures allows cross-lane chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest that `StackerDBChunkData` signatures commit to — binds only to `slot_id`, `slot_version`, and `data_hash`. It does not include the StackerDB contract identifier (analogous to EIP-712's missing `version` domain field). Because every `signers-{0,1}-{message_id}` StackerDB contract for a given signer_set shares the *identical* signer-to-slot assignment (`signers.clar`'s `stackerdb-get-signer-slots-page` is keyed only by signer_set, not by `message_id`), a chunk validly signed by a signer for one message lane (e.g. `signers-X-1`, BlockResponse) is also a validly-verifying chunk for the *same slot_id* in every other message lane of that signer_set (`signers-X-0`, `signers-X-2` ... `signers-X-7`), since `SlotMetadata::verify()` only checks that the recovered pubkey hashes to the configured slot owner, with no contract/lane binding.

### Finding Description
`SlotMetadata::auth_digest()` in [1](#0-0)  computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash`. `StackerDBChunkData::sign`/`verify` reuse this same digest [2](#0-1) , and neither of these routines mixes in the target contract's `QualifiedContractIdentifier`.

The signer-to-slot mapping is derived from `.signers` boot contract state that is shared across all `signers-{signer_set}-{message_id}` lane contracts for that signer_set — `stackerdb-get-signer-slots-page` returns the same list regardless of `message_id` [3](#0-2) , and each lane contract simply forwards to that shared page [4](#0-3) . So the same signer address owns the same `slot_id` in every one of the 8 message-id lane contracts for a signer_set (`MessageSlotID`/`SignerMessageTypePrefix` enumerates BlockProposal=0, BlockResponse=1, BlockPushed=2, MockProposal=3, MockSignature=4, MockBlock=5, StateMachineUpdate=6, BlockPreCommit=7) [5](#0-4) .

At the network layer, `PeerNetwork::validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs` only looks up the slot owner for the specific `smart_contract_id` passed in and calls `slot_metadata.verify(&addr)` [6](#0-5) . Because the signature digest never encodes which contract it was meant for, and the owner-address-per-slot is identical across all lane contracts of a signer_set, a signature legitimately produced for lane A verifies successfully when replayed (with the identical `slot_id`/`slot_version`/`data`/`sig` bytes) against a completely different lane contract B. This same `validate_received_chunk` path is invoked from `handle_unsolicited_StackerDBPushChunk`, which is reachable from any connected, unauthenticated p2p peer [7](#0-6) .

This is a direct structural analog of the reported bug: just as omitting `version` from `EIP712Domain` collapses distinct signing domains into one (letting a signature meant for one context be replayed in another where the same signer/verifying party is valid), omitting the StackerDB contract identity from `auth_digest()` collapses all message-lane StackerDB "domains" for a signer_set into one signing domain.

### Impact Explanation
Any network peer that observes a legitimately-signed `StackerDBChunkData` (broadcast/pushed for one message lane, e.g. `signers-X-1`) can immediately re-push the identical bytes as an unsolicited `StackerDBPushChunkData` targeting a different lane contract's stackerdb (`signers-X-0`, `signers-X-6`, etc.), and it will pass `validate_received_chunk`'s signature check and be accepted/stored/rebroadcast, because the same signer owns the same `slot_id` in every lane and the digest carries no lane/contract binding. This is an unauthorized write into a StackerDB replica the attacker does not control the keys for, achieved purely by replaying a signature across domains — matching the "StackerDB chunk stored without a valid owner signature [for that specific contract]" class in scope. Downstream, `SignerEvent::TryFrom<StackerDBChunksEvent>` does filter by payload type-byte per lane (`signer_message_payload_matches_lane`) before constructing typed `SignerMessage`s, which limits (but does not eliminate) the blast radius at the application-message-interpretation layer — but the raw chunk is still accepted and persisted into the wrong StackerDB slot at the storage/replication layer regardless, corrupting/overwriting that slot's legitimate version-tracked state and propagating to other replicas.

### Likelihood Explanation
High for a remote, unprivileged actor: the attacker needs no private key and no elevated privileges — only to observe one broadcast chunk (trivial, since StackerDB chunks are gossiped) and re-send it as an unsolicited push to any peer, using the standard StackerDB push/gossip path that is open to any connected peer.

### Recommendation
Bind the signed digest to the specific StackerDB replica: include the `QualifiedContractIdentifier` (or an equivalent stable domain identifier such as `(contract_id, message_id)`) inside `SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs`, and thread the contract identifier through `StackerDBChunkData::sign`/`verify`/`recover_pk`, updating all callers (`stackslib/src/net/stackerdb/mod.rs::validate_received_chunk`, the `/v2/stackerdb/.../chunks` POST handler, and `libsigner`/`stacks-signer` signing code) to pass the destination contract ID into the digest computation, exactly as EIP-712 recommends including `version`/domain fields to prevent cross-domain signature reuse.

### Proof of Concept
1. Signer S is a member of signer_set X and owns `slot_id = 3` in every `signers-X-{0..7}` lane contract (guaranteed by `signers.clar`'s shared `stackerdb-get-signer-slots-page`).
2. S legitimately signs and pushes a `StackerDBChunkData{slot_id:3, slot_version:5, data: D}` to lane `signers-X-1` (BlockResponse). This is observed by attacker A on the p2p network.
3. A crafts a `StackerDBPushChunkData{contract_id: signers-X-6 (StateMachineUpdate), rc_consensus_hash: <current>, chunk_data: <identical bytes from step 2>}` and sends it (unsolicited) to any peer's `PeerNetwork::handle_unsolicited_StackerDBPushChunk`.
4. `validate_received_chunk` looks up the slot-3 owner for `signers-X-6` (same address as for `signers-X-1`), calls `slot_metadata.verify(&addr)`, which succeeds because `auth_digest()` never referenced `signers-X-1` vs `signers-X-6`.
5. The chunk is accepted as version 5 for slot 3 in `signers-X-6`, overwriting/poisoning that unrelated lane's StackerDB state, without S ever having signed anything for that lane.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L160-166)
```rust
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }
```

**File:** libstackerdb/src/libstackerdb.rs (L223-244)
```rust
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** libsigner/src/v0/messages.rs (L104-123)
```rust
define_u8_enum!(
/// Enum representing the SignerMessage type prefix
SignerMessageTypePrefix {
    /// Block Proposal message from miners
    BlockProposal = 0,
    /// Block Response message from signers
    BlockResponse = 1,
    /// Block Pushed message from miners
    BlockPushed = 2,
    /// Mock block proposal message from Epoch 2.5 miners
    MockProposal = 3,
    /// Mock block signature message from Epoch 2.5 signers
    MockSignature = 4,
    /// Mock block message from Epoch 2.5 miners
    MockBlock = 5,
    /// State machine update
    StateMachineUpdate = 6,
    /// Block Pre-commit message
    BlockPreCommit = 7
});
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

Confirmed: every `signers-0-{message_id}` (and `signers-1-{message_id}`) StackerDB contract delegates its `stackerdb-get-signer-slots` to `.signers stackerdb-get-signer-slots-page u0` (same for signer-set 1). This means, for a given reward cycle, every message-lane contract (`signers-0-1`, `signers-0-2`, `signers-0-3`, …) assigns the *same* signer address to the *same* `slot_id`, because they all read the identical slot-assignment table from the shared `.signers` contract. That confirms the cross-contract slot collision precondition needed for the analog below.

### Title
StackerDB chunk signatures omit the contract identity, allowing valid signer chunks to be replayed across StackerDB instances - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` binds a StackerDB chunk signature only to `(slot_id, slot_version, data_hash)` and never to the `QualifiedContractIdentifier` of the StackerDB it was written to. Because every `signers-0-{message_id}` and `signers-1-{message_id}` contract assigns identical `(signer, slot_id)` mappings (they all call through to the shared `.signers` contract's `stackerdb-get-signer-slots-page`), a chunk validly signed by a real signer for one message-lane contract passes signature verification (`SlotMetadata::verify`) for every *other* contract in which that signer owns the same `slot_id`, with no binding to which contract the signature was meant to authorize.

### Finding Description
`SlotMetadata::auth_digest()`:
```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
``` [1](#0-0) 

This digest is what gets signed (`sign`) and verified (`verify`) against a `StacksAddress` [2](#0-1) . Note it contains no `contract_id`/StackerDB identifier at all — only slot id, slot version, and data hash.

`StackerDBs::try_replace_chunk` performs the write-path validation:
```rust
let slot_validation = self.get_slot_validation(smart_contract, slot_desc.slot_id)?...;
if !slot_desc.verify(&slot_validation.signer)? {
    return Err(net_error::BadSlotSigner(...));
}
``` [3](#0-2) 

The `smart_contract` parameter is used only to look up *which signer is expected to own this slot in this DB* — it is never folded into the signed digest. So `verify()` cannot distinguish "signer X authorized this chunk for contract A" from "signer X authorized this chunk for contract B"; it only checks that the recovered pubkey hash equals whichever address happens to own `slot_id` in the target contract.

This is exploitable because the per-reward-cycle signer-slot assignment is shared across lanes:
```clarity
(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))
``` [4](#0-3) 

i.e. every `signers-0-{message_id}` contract for the same reward cycle (`BlockResponse`, `StateMachineUpdate`, `MockProposal`, etc. lanes) gives the *same signer the same slot_id*. Consequently, any unprivileged network relay that observes a legitimately-signed, legitimately-broadcast chunk for `signers-0-1.slot_id=5,version=V` can resubmit the exact same `(slot_id, slot_version, sig, data)` tuple to a *different* lane contract, e.g. `signers-0-2`, via `PUT/POST /v2/stackerdb/<contract>/chunks` or via `StackerDBPushChunkData` p2p gossip. As long as slot 5 in `signers-0-2` is also freshly writable (version check passes) and is owned by the same signer address, `try_replace_chunk`/`validate_received_chunk` will accept and store it as an authentic write to the *wrong* StackerDB/lane — the chunk was never actually authorized for that contract by the signer.

`PeerNetwork::validate_received_chunk` has the identical gap for the gossip/replication path: it verifies signature only against `get_slot_signer(smart_contract_id, slot_id)`, again without any contract binding in the signed bytes [5](#0-4) .

This exactly parallels the Cally `createVault()` bug class: a declared/contextual "type" (here: which StackerDB contract the chunk is authorized for) is never checked for equality against what's actually bound into the authenticated payload, so data legitimately produced under one type/context is accepted and persisted under a different type/context.

### Impact Explanation
This allows any unprivileged network peer (no signer private key required) to force acceptance of forged-context data into a StackerDB contract other than the one it was actually signed for, as long as the victim signer occupies the same slot index in both contracts (guaranteed by the shared `.signers` slot table for same-cycle message lanes). This is an unauthorized write to StackerDB state / propagation of forged data across the network — matching the Critical impact category ("unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data").

Downstream, `libsigner`'s `signer_message_payload_matches_lane` filter (comment explicitly flags it as "fixed to v0 semantics" and can silently fail to recognize newer variants) may catch some but not all cross-lane replays at the consumption layer [6](#0-5) ; even where it does filter, the underlying StackerDB storage layer (`db.rs`, `mod.rs`) has already accepted and persisted the wrongly-scoped chunk as if it were correctly authorized, corrupting the DB's slot_version/data state and potentially still being consumed by any component (present or future) that doesn't re-derive/re-check the type byte.

### Likelihood Explanation
High. No secrets are needed by the attacker — only passive observation of one legitimately broadcast, signed chunk (trivial, since these are gossiped/served over the p2p network and HTTP StackerDB endpoints) plus a single unauthenticated write request to a different contract endpoint. The precondition (same signer holding the same slot_id across sibling message-lane contracts within a reward cycle) is guaranteed by design via the shared `.signers`/`stackerdb-get-signer-slots-page` indirection.

### Recommendation
Include the target StackerDB's `QualifiedContractIdentifier` (and ideally a network/chain-view discriminator already used elsewhere, e.g. reward cycle) inside `SlotMetadata::auth_digest()` so that signatures are cryptographically bound to the specific StackerDB contract they authorize, e.g.:
```rust
hasher.update(contract_id.serialize_to_vec());
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
```
This requires threading `contract_id` through `sign`/`verify`/`get_slot_metadata` (in `libstackerdb.rs`) and updating all callers (`StackerDBs::try_replace_chunk`, `PeerNetwork::validate_received_chunk`, HTTP `poststackerdbchunk` handler, and any signer-side re-signing code) to pass the contract id consistently. Existing on-chain/off-chain signed data would need a migration/versioning bump since this changes the signed message format.

### Proof of Concept
1. Reward cycle N is active; `.signers` assigns `signer_addr` to `slot_id = 5` for signer-set 0 (shared by `signers-0-1`, `signers-0-2`, …).
2. `signer_addr` legitimately signs and broadcasts a chunk `C = {slot_id: 5, slot_version: 3, sig, data}` intended for `signers-0-1` (e.g., a `BlockResponse` message), as shown in `test_handle_unsolicited_stackerdb_push_chunk_future_view_validation`-style flows [7](#0-6) .
3. An unprivileged network peer observes `C` (via gossip or `GET /v2/stackerdb/signers-0-1/chunks/5`).
4. That peer submits the identical `C` (same `sig`, same `slot_version=3` if slot 5 in `signers-0-2` is still at an older version, or with the version check satisfied) to `signers-0-2` via `POST /v2/stackerdb/signers-0-2/chunks` or as a `StackerDBPushChunkData` p2p message.
5. `StackerDBs::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:411-423`) looks up `slot_validation.signer` for `signers-0-2`/slot 5, finds it equals `signer_addr` (same table via `.signers`), calls `slot_desc.verify(&slot_validation.signer)`, which succeeds because `auth_digest()` never included the contract id — the write is accepted and stored under `signers-0-2` even though `signer_addr` never authorized that specific contract to hold this chunk.

**Uncertainty / unverified aspects:** I could not execute this against a running node to observe the exact runtime consequence (e.g., whether a specific downstream signer state machine misbehaves as a result), and I did not exhaustively confirm every call site that constructs/consumes `SlotMetadata`/`StackerDBChunkData` signatures (there may be additional guard checks elsewhere in `stackslib/src/net/api/poststackerdbchunk.rs` not fully reviewed). The core root cause — `auth_digest()` excluding contract identity — is directly confirmed in code, and the cross-lane slot-collision precondition is confirmed via the `.clar` contracts.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
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

**File:** libsigner/src/events.rs (L576-596)
```rust
            // NOTE: the payload-type check below uses v0 `SignerMessageTypePrefix` semantics
            // (the mapping in `signer_message_payload_matches_lane` is fixed to v0). Future
            // signer-message versions must extend that mapping, or their chunks will not be
            // recognized here regardless of which `T` is in scope.
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
```

**File:** stackslib/src/net/tests/relay/nakamoto.rs (L1224-1247)
```rust
    // --- Test 1: Properly signed chunk should be BUFFERED on the FutureView path ---
    let mut good_chunk_data = StackerDBPushChunkData {
        contract_id: contract_id.clone(),
        rc_consensus_hash: future_consensus_hash.clone(),
        chunk_data: StackerDBChunkData::new(0, 1, vec![1, 2, 3, 4, 5]),
    };
    good_chunk_data.chunk_data.sign(&signer_privk).unwrap();

    let result = peer
        .network
        .handle_unsolicited_StackerDBPushChunk(
            &mut stacks_node.chainstate,
            1,
            &preamble,
            &good_chunk_data,
            false,
        )
        .unwrap();

    assert_eq!(
        result,
        (true, false),
        "chunk with valid signature must be buffered on FutureView path"
    );
```

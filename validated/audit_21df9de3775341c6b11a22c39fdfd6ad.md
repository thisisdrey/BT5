Confirmed: `stackerdb-get-signer-slots` in `signers-0-xxx.clar` and `signers-1-xxx.clar` both delegate to the single `.signers` contract's `stackerdb-get-signer-slots-page`, so every `.signers-{cycle%2}-{message_id}` StackerDB contract for a given cycle-parity shares the *identical* `(signer, slot_id)` assignment across all message-id lanes (`BlockProposal`, `BlockResponse`, `BlockPushed`, `MockProposal`, `MockSignature`, `MockBlock`, `StateMachineUpdate`, `BlockPreCommit`). This confirms the cross-contract replay condition.

### Title
Signed StackerDB chunk digest lacks a domain separator, enabling cross-contract signature replay across `.signers-*` message lanes - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` — the digest signed and verified for every StackerDB chunk write — commits only to `(slot_id, slot_version, data_hash)` and omits any binding to the StackerDB contract identity, message lane, or network. Because all `.signers-{cycle%2}-{message_id}` contracts for a given reward-cycle parity share the exact same `(signer, slot_id)` assignment, a valid signature captured from one message-id lane can be replayed to store the identical chunk into a slot in a *different* message-id contract, since the cryptographic check never distinguishes which contract or lane it was authorized for.

### Finding Description
`SlotMetadata::auth_digest` in `libstackerdb/src/libstackerdb.rs` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

This digest is what gets signed (`SlotMetadata::sign`) and verified (`SlotMetadata::verify`): [2](#0-1) 

Verification for both HTTP-posted chunks and gossiped push-chunks calls this same `verify`/`auth_digest` path without ever mixing in the target `smart_contract_id`: [3](#0-2) 

The database write path (`try_replace_chunk`) resolves the expected signer purely from `(smart_contract_id, slot_id)` via `get_slot_signer`, but the signature itself was never bound to that `smart_contract_id`: [4](#0-3) 

Critically, the boot contracts show that all message-lane contracts for a reward-cycle parity share one signer/slot assignment: `signers-0-xxx.clar` (and identically `signers-1-xxx.clar`) simply forward to the shared `.signers` contract's page function: [5](#0-4) [6](#0-5) 

And the lane/message-id contracts are literally `signers-{cycle%2}-{message_id}` for `message_id` in `{BlockProposal, BlockResponse, BlockPushed, MockProposal, MockSignature, MockBlock, StateMachineUpdate, BlockPreCommit}`: [7](#0-6) [8](#0-7) 

Because every one of these lane contracts for a given cycle parity assigns signer `S` the same `slot_id`, and the signed digest never encodes the contract identity/message-lane, a `StackerDBChunkData` signed and legitimately posted by `S` to `.signers-X-1` (e.g. `BlockResponse`) at `(slot_id=k, slot_version=v, data=D)` produces a signature that is *equally valid* when replayed verbatim as a POST to `.signers-X-6` (e.g. `StateMachineUpdate`) at the same `slot_id=k`, provided the target lane's slot version for `k` is `<= v` (trivially true at low/early versions, e.g. version 1, which is common right after a reward-cycle handoff when all lane slots reset toward version 0). `validate_received_chunk`/`try_replace_chunk` will accept it as a valid, freshly-signed write, and — since the HTTP handler re-broadcasts every accepted chunk via `StackerDBPushChunk` — the mis-attributed chunk is relayed network-wide to peers as authentic content for that lane: [9](#0-8) 

The receiving `SignerEvent` decoder does filter payload-type bytes against the expected lane (`signer_message_payload_matches_lane`), which mitigates *misinterpretation* by conforming signer software, but this is an application-heuristic filter, not a cryptographic guarantee, and it does not prevent the storage-layer write/overwrite or the P2P propagation of the mis-lane chunk itself: [10](#0-9) 

### Impact Explanation
This breaks the intended guarantee that a StackerDB chunk signature authorizes a write only to the specific `(contract, slot)` context it was produced for. An unprivileged network participant who observes any valid, previously-broadcast `StackerDBChunkData` (which are gossiped in the clear over P2P/HTTP) can replay it into a sibling `.signers-X-*` contract's slot owned by the same signer, causing an unauthorized/forged write to that StackerDB's stored state and its network-wide propagation via the automatic push-chunk relay — matching the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact class. It could also be used to overwrite a lane's chunk with stale/foreign data to disrupt bookkeeping or waste an early slot-version window before the legitimate signer message arrives (a light griefing/DoS on that specific lane's freshness).

### Likelihood Explanation
Exploitation requires only observing a legitimately signed chunk (trivial — chunks are broadcast in plaintext to all peers) and replaying it against a different, but topologically related, contract with a compatible (low/equal) slot version, which is common at the start of an activation of a reward-cycle-parity's contract set (fresh slots start at version 0). No secret key, admin role, or node compromise is required — only a valid signer's already-published chunk and standard unauthenticated write access to the `POST /v2/stackerdb/.../chunks` endpoint or P2P `StackerDBPushChunk` handler.

### Recommendation
Bind the signed digest to a full domain: include the StackerDB `QualifiedContractIdentifier` (and ideally the network/chain id, e.g. `mainnet`/`chain_id`) inside `SlotMetadata::auth_digest`, so a signature over one contract's slot cannot be replayed onto another contract's identically-indexed slot. This requires updating `auth_digest`, `sign`, and `verify` in `libstackerdb/src/libstackerdb.rs`, plus threading the contract id (and network id) into all call sites (`validate_received_chunk`, `try_replace_chunk`, and the `StackerDBChunkData`/`StackerDBPushChunkData` protocol) with a corresponding protocol version bump.

### Proof of Concept
1. Signer `S` legitimately signs and posts a `StackerDBChunkData{slot_id: 3, slot_version: 1, data: D}` to `.signers-0-1` (`BlockResponse` lane); the node accepts it and relays it via `StackerDBPushChunk`.
2. An attacker observes this chunk on the wire (it is broadcast unauthenticated to all peers/relays).
3. The attacker crafts an HTTP POST to `/v2/stackerdb/<addr>/signers-0-6/chunks` (the `StateMachineUpdate` lane for the same cycle parity), reusing the *exact same* `slot_id=3`, `slot_version=1`, `sig`, and `data=D` from step 1.
4. Because `.signers-0-6` shares the same `(signer, slot_id)` mapping as `.signers-0-1` (both delegate to `.signers.stackerdb-get-signer-slots-page`), `get_slot_signer` for `.signers-0-6` slot 3 also resolves to `S`.
5. `try_replace_chunk` → `validate_received_chunk` → `SlotMetadata::verify` recomputes `auth_digest()` from `(slot_id=3, slot_version=1, data_hash(D))` only — identical to step 1's digest — so the signature verifies successfully against `S`, even though `S` never signed anything for `.signers-0-6`.
6. The chunk is accepted, stored, and re-broadcast to the network as if it were an authentic `StateMachineUpdate` chunk from `S`, even though `S` only ever authorized it for the `BlockResponse` lane.

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

**File:** stackslib/src/net/stackerdb/db.rs (L534-543)
```rust
    pub fn get_slot_signer(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_id: u32,
    ) -> Result<Option<StacksAddress>, net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "SELECT signer FROM chunks WHERE stackerdb_id = ?1 AND slot_id = ?2";
        let args = params![stackerdb_id, slot_id];
        query_row(&self.conn, sql, args).map_err(|e| e.into())
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** libsigner/src/v0/messages.rs (L104-134)
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

#[cfg_attr(test, mutants::skip)]
impl MessageSlotID {
    /// Return the StackerDB contract corresponding to messages of this type
    pub fn stacker_db_contract(
        &self,
        mainnet: bool,
        reward_cycle: u64,
    ) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1073)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
    }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-323)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
```

**File:** libsigner/src/events.rs (L568-619)
```rust
        } else if event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot() {
            let Some((signer_set, message_id)) =
                get_signers_db_signer_set_message_id(event.contract_id.name.as_str())
            else {
                return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
            };
            // signer-XXX-YYY boot contract
            //
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
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    let Ok(message) = read_next::<T, _>(&mut &chunk.data[..]) else {
                        warn!(
                            "Skipping signer chunk: payload deserialization failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    Some((chunk.slot_id, pk, message))
                })
                .collect();
            SignerEvent::SignerMessages {
                signer_set,
                messages,
                received_time,
            }
```

### Title
StackerDB chunk signatures are not bound to the target smart contract, enabling cross-StackerDB replay ("signature wrapping") of validly-signed chunks - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the value that gets signed and later verified for every StackerDB chunk write. It only commits to `slot_id`, `slot_version`, and `data_hash` — it never commits to the target StackerDB contract (`QualifiedContractIdentifier`). Because the same signer set is deployed to many distinct StackerDB contracts (one per message type, e.g. `signers-0-0` through `signers-0-N` and `signers-1-0` through `signers-1-N`), the same `StacksAddress` is assigned the same `slot_id` across all of these sibling contracts. A signature that is valid for a chunk in one contract is therefore also valid for the identical `(slot_id, slot_version, data)` triple in every other contract where that signer owns the same slot — a classic "signature wrapping" condition: the cryptographic envelope is intact, but the context (which document/contract the signature was intended for) is not authenticated.

### Finding Description
`SlotMetadata::auth_digest` hashes only three fields: [1](#0-0) 

`verify()` recovers the public key from this digest and only checks that the recovered key hashes to the expected `principal` (the per-slot owner address for the target contract) — the contract identity plays no role in the signed data: [2](#0-1) 

The write path (`StackerDBSync::validate_downloaded_chunk`, `PeerNetwork::validate_received_chunk`, and the `POST /v2/stackerdb/...` RPC handler) resolves the expected signer strictly by `(smart_contract_id, slot_id)` via `get_slot_signer`, then calls `slot_metadata.verify(&addr)`: [3](#0-2) 

Slot assignment for the `.signers-*` family of contracts is derived purely from `stackerdb-get-signer-slots-page`, which returns the same ordered list of signer addresses (hence identical `slot_id` per address) for *every* message-type contract belonging to a signer set: [4](#0-3) [5](#0-4) 

`MessageSlotID` documents exactly this design — one contract per message type, all sharing the signer-index-based slot layout, and the message type is only distinguished by an in-band `SignerMessageTypePrefix` inside the payload, not by anything the signature covers: [6](#0-5) 

Because `auth_digest` binds neither the `smart_contract_id` nor the `MessageSlotID`/message-type prefix, an attacker who observes a legitimately-signed chunk broadcast for one message-type StackerDB (e.g. `signers-0-<BlockResponse>`) can resubmit the exact same `(slot_id, slot_version, data, sig)` tuple to a sibling StackerDB (e.g. `signers-0-<BlockPreCommit>` or `signers-0-<StateMachineUpdate>`) for the same signer set. Since `get_slot_signer` for the target contract returns the same address (identical slot ordering) and `verify()` only checks that unscoped triple, the chunk is accepted by `validate_received_chunk`/`try_replace_chunk` and stored and gossiped as if it were legitimately destined for and authored for that contract.

### Impact Explanation
This breaks the intended equality "signed for contract A" == "valid only in contract A." An unprivileged network peer (StackerDB chunks are gossiped) can:
- Force acceptance and network-wide propagation of a signer's data into a message-type StackerDB slot it was never intended for, corrupting the per-message-type view that miners/signers rely on (e.g., `StackerDBListener` reads slots expecting a specific `SignerMessageV0` variant per contract).
- At minimum, cause chunk-store confusion/DoS as the "wrong" content occupies a slot in another contract, potentially blocking the legitimate signer from having its real message for that channel accepted (stale/duplicate-version checks could reject the genuine, later write, effectively censoring or drowning out a signer's true message for that lane).
- If any two message types are structurally compatible (e.g., both begin with fields that parse validlaly under `SignerMessageTypePrefix`+`StacksMessageCodec`), a replayed chunk could be misinterpreted as a legitimate different-type signer message, i.e., real type confusion feeding forged data into consensus-adjacent signer state (`StackerDBListener`, `stacks-signer` runloop).

This matches the "network-wide propagation of forged/misattributed data" and "unauthenticated write into the wrong StackerDB context" categories in scope, achievable by any peer with no special privileges, using only data it can passively observe from normal StackerDB gossip.

### Likelihood Explanation
High feasibility: StackerDB chunk contents and their signatures are broadcast in the clear as part of normal peer-to-peer replication (that is the entire point of StackerDB), so an attacker does not need to compromise any key — only to capture a chunk gossiped for one message-type contract and resubmit it (via `POST /v2/stackerdb/<contract>/chunks` or via `StackerDBPushChunk` P2P messages) against a sibling contract for the same signer set. The only constraints are that the target contract's config accepts the `slot_version` as "current or newer" and the `chunk_size`/`max_writes` bounds, all of which are attacker-observable/controllable within normal protocol limits.

### Recommendation
Include the `smart_contract_id` (and ideally the expected `MessageSlotID`/message-type identifier) inside `SlotMetadata::auth_digest()` so a signature is only valid for a specific StackerDB contract and cannot be replayed across sibling contracts that reuse the same slot ordering. This requires a coordinated signing/verification change across `libstackerdb::SlotMetadata::{sign,verify,auth_digest}` and all callers that construct/verify `StackerDBChunkData`.

### Proof of Concept
1. Observe a validly-signed StackerDB chunk `(slot_id=S, slot_version=V, data=D, sig=SIG)` broadcast for contract `signers-0-1` (`BlockResponse`), where `SIG` is produced over `auth_digest = H(S || V || H(D))` per [1](#0-0) .
2. Confirm the address owning slot `S` in `signers-0-1` also owns slot `S` in `signers-0-2` (`StateMachineUpdate`), since both derive their slot list from the same `stackerdb-get-signer-slots-page` call, per [4](#0-3) .
3. Submit `StackerDBChunkData{slot_id: S, slot_version: V, sig: SIG, data: D}` to `signers-0-2` via the `POST /v2/stackerdb/<signers-0-2>/chunks` RPC or an unsolicited `StackerDBPushChunk` P2P message.
4. The node calls `validate_received_chunk`, resolves `get_slot_signer(signers-0-2, S)` to the same address, and `slot_metadata.verify(&addr)` succeeds because the digest never referenced the contract, per [3](#0-2) . The chunk is accepted into `signers-0-2`'s slot `S` and re-gossiped, despite never having been signed for that contract.

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

**File:** libsigner/src/v0/messages.rs (L68-96)
```rust
define_u8_enum!(
/// Enum representing the stackerdb message identifier: this is
///  the contract index in the signers contracts (i.e., X in signers-0-X)
MessageSlotID {
    /// Block Response message from signers
    BlockResponse = 1,
    /// Signer State Machine Update
    StateMachineUpdate = 2,
    /// Block Pre-commit message from signers before they commit to a block response
    BlockPreCommit = 3
});

define_u8_enum!(
/// Enum representing the slots used by the miner
MinerSlotID {
    /// Block proposal from the miner
    BlockProposal = 0,
    /// Block pushed from the miner
    BlockPushed = 1
});

impl MessageSlotIDTrait for MessageSlotID {
    fn stacker_db_contract(&self, mainnet: bool, reward_cycle: u64) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }
    fn all() -> &'static [Self] {
        MessageSlotID::ALL
    }
}
```

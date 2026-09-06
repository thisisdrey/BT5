## Analog Found

### Title
Missing StackerDB "context" binding in `SlotMetadata` signatures allows cross-database chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest as `hash(slot_id || slot_version || data_hash)`, with no commitment to *which* StackerDB (smart contract) the chunk is intended for. Because the Stacks signer set assigns the same signer address to the same numeric `slot_id` across multiple, distinct StackerDB replicas (`signers-0-1`/`signers-0-2`/`signers-0-3`, etc., one per `MessageSlotID`), a validly-signed chunk observed for one database can be replayed verbatim into a sibling database at the same slot, exactly the way ZITADEL's `CodeExchange`/`RefreshToken` validated the credential owner but not which client the grant was issued to.

### Finding Description
The signature that authenticates a StackerDB chunk write is computed by: [1](#0-0) 
This digest binds only `slot_id`, `slot_version`, and `data_hash`. It never binds the target `QualifiedContractIdentifier` (i.e., the specific StackerDB "database"/client context) that the chunk is destined for.

Both the write path and the P2P-relay validation path check the signature exclusively against the signer address stored for `(smart_contract, slot_id)`, but do not fold the contract identifier into what was actually signed: [2](#0-1) [3](#0-2) 

Critically, the signer-to-slot assignment is shared identically across multiple sibling StackerDB contracts for the same signer set: `signers-0-1` (BlockResponse), `signers-0-2` (StateMachineUpdate), and `signers-0-3` (BlockPreCommit) all pull the exact same per-signer slot list from `.signers`: [4](#0-3) [5](#0-4) 

So signer `K` occupying `slot_id = i` in the BlockResponse StackerDB also occupies `slot_id = i` in the StateMachineUpdate and BlockPreCommit StackerDBs of the same reward-cycle signer set (`make_signers_db_contract_id` in `stackslib/src/chainstate/nakamoto/signer_set.rs:1061-1073`). Because the signature never commits to the contract, a chunk `(slot_id=i, slot_version=v, data, sig)` that was legitimately signed by `K` for `signers-0-1` remains a perfectly valid signature when re-submitted (via `POST /v2/stackerdb/.../chunks`, `stackslib/src/net/api/poststackerdbchunk.rs`) or pushed over the P2P `StackerDBPushChunk` gossip path for `signers-0-2` or `signers-0-3` at the same `slot_id`, provided the target replica's stored `slot_version` for that slot is lower than `v`.

### Impact Explanation
An attacker who merely observes on-the-wire chunk data/signatures being broadcast for one message channel (no private key needed — chunks and their signatures are inherently public gossip/HTTP-readable data) can cross-post that exact same signed payload into a different, unrelated StackerDB message channel for the same signer set, as long as version ordering permits the write. This lets an unprivileged network participant inject attacker-selected (but signer-authenticated) bytes into a slot of a database that signer never intended to write to, and have that forged-context chunk accepted and further relayed/propagated by other nodes as a legitimate `BlockPreCommit`/`StateMachineUpdate`/`BlockResponse` payload — satisfying "unauthenticated/unauthorized write to state" and "network-wide propagation of forged data" per the acceptable-impact classes. Consumers such as `stacks-node/src/nakamoto_node/stackerdb_listener.rs` deserialize StackerDB chunk contents with type-specific codecs (`SignerMessageV0::consensus_deserialize`); cross-channel injected bytes that happen to deserialize (or partially parse before failing) can desynchronize the mislead-target message-type's downstream state (e.g., global state evaluator, block pre-commit tallies) for that signer, without that signer ever having signed anything intended for that channel.

### Likelihood Explanation
Exploitation requires only replaying already-public data (the signature and content of a chunk broadcast for one channel) into a different channel via a normal, unauthenticated StackerDB write path — no cryptographic material or privileged access is needed, only correct ordering with respect to slot versions in the target database (attacker only needs the target's current version, obtainable by a normal read). This is a low-complexity, remote, unauthenticated action, matching the "High" bug class described in the external report (bound-check omission enabling cross-context replay of otherwise-valid credentials/signatures).

### Recommendation
Bind the target StackerDB context into what is actually signed: include the `QualifiedContractIdentifier` (or a fixed contract/reward-cycle/message-type identifier) inside `SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs`, and verify that binding on both the local write path (`StackerDBTx::try_replace_chunk` / `get_slot_validation` in `stackslib/src/net/stackerdb/db.rs`) and the P2P chunk-validation path (`validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs`), mirroring the ZITADEL fix of re-introducing strict client/context identity validation.

### Proof of Concept
1. Signer `K` is registered with `slot_id = i` in reward-cycle signer set 0 for message types `BlockResponse` (`signers-0-1`), `StateMachineUpdate` (`signers-0-2`), and `BlockPreCommit` (`signers-0-3`) — guaranteed by shared `stackerdb-get-signer-slots-page` in `signers.clar`.
2. `K` legitimately signs and pushes chunk `C = (slot_id=i, slot_version=v, data=D)` to `signers-0-1` (BlockResponse). This is observable on the network (gossip push, or via the public read RPC).
3. Attacker (no keys) takes the exact same `(slot_id, slot_version, data, sig)` tuple and POSTs it to `signers-0-2`'s `/v2/stackerdb/{signers-0-2}/chunks` endpoint (or relays it via `StackerDBPushChunk` P2P message with `contract_id = signers-0-2`).
4. `try_replace_chunk`/`validate_received_chunk` look up the signer for `(signers-0-2, slot_id=i)` — which is the same `K` — and `slot_desc.verify(&K)` succeeds because `auth_digest()` never included the contract id, so the signature is indistinguishable from one legitimately made for `signers-0-2`.
5. As long as `signers-0-2`'s stored version for slot `i` is `< v`, the write succeeds, storing attacker-replayed `BlockResponse` data as if it were a `StateMachineUpdate` chunk signed by `K`, and this write further gets gossiped as legitimate to peers.

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** libsigner/src/v0/messages.rs (L68-92)
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
```

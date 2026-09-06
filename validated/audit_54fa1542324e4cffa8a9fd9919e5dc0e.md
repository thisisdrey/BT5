### Title
StackerDB chunk signatures do not bind to the target contract, enabling cross-`StackerDB` chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the digest that signer keys sign over as `hash(slot_id || slot_version || data_hash)`, omitting the identity of the StackerDB (`QualifiedContractIdentifier`) the chunk is destined for. Because the `.signers-0-1`, `.signers-0-2`, and `.signers-0-3` (and `.signers-1-*`) contracts all derive their slot-to-signer mapping from the very same page in the `.signers` boot contract, a chunk validly signed for one message-type StackerDB is also a validly "signed" chunk for the sibling StackerDB(s) at the same slot id. A chunk observed on one contract's replication stream can therefore be relayed and accepted into a different contract's replica.

### Finding Description
The signed digest is: [1](#0-0) 

It covers only `slot_id`, `slot_version`, and `data_hash` — never the smart-contract identifier of the StackerDB being written to. Verification during ingestion mirrors this: the node looks up the "expected signer" purely from `(smart_contract_id, slot_id)` and checks the digest against that address, again without folding `smart_contract_id` into what was actually signed: [2](#0-1) [3](#0-2) 

The slot-to-signer assignment for the signer message contracts is shared verbatim across sibling contracts of the same parity. `signers-0-xxx.clar` (used for `BlockResponse`=1, `StateMachineUpdate`=2, `BlockPreCommit`=3) all call the same read-only function: [4](#0-3) 

which is served by a single shared page in `.signers`: [5](#0-4) 

So for a given reward-cycle parity, the same `StacksAddress` occupies the identical `slot_id` in `signers-0-1`, `signers-0-2`, and `signers-0-3` simultaneously (and likewise for `signers-1-*`). Since a chunk's signature says nothing about *which* of these three contracts it was meant for, a signed `StackerDBChunkData` gossiped for `signers-0-1` (a `BlockResponse` message) passes `validate_received_chunk`/`try_replace_chunk` just as well when replayed against `signers-0-2` (a `StateMachineUpdate` slot) or `signers-0-3` (`BlockPreCommit`), as long as its `slot_id` and `slot_version` clear the freshness/`max_writes` checks for that sibling replica. This is the direct analog of the reported bug class: the signed structure fails to bind the context (here: which StackerDB contract) it is valid for, exactly like a merkle leaf that omits `questID`/`period`.

Any peer that legitimately receives a signer's chunk via P2P StackerDB push/relay (`handle_unsolicited_StackerDBPushChunk`) or download sync can re-post that exact `(slot_id, slot_version, sig, data)` tuple against a sibling contract via the public, unauthenticated `POST /v2/stackerdb/{principal}/{contract_name}/chunks` RPC endpoint: [6](#0-5) 

Because `chunk.sig`/`SlotMetadata::verify` cannot distinguish "signed for contract A" from "signed for contract B", the write succeeds and the node stores the misdirected data (and correspondingly bumps its slot version), which the node's application layer will then try to parse as a message of the wrong `MessageSlotID`.

### Impact Explanation
This is an unauthenticated-relative-to-context write: an attacker with no signer key can inject a chunk that a real signer key legitimately produced, but for a different logical channel, into a StackerDB replica it was never intended for. Effects reachable without any privileged key:
- Version-bump griefing/DoS: replaying a chunk with a high `slot_version` into the wrong contract advances that slot's version, so the legitimate future write for that (contract, slot) with a lower/equal version is rejected as `StaleChunk`, blocking propagation of the real signer message for that slot (e.g., blocking a `StateMachineUpdate` or `BlockPreCommit`) until the real signer catches up its version numbering — degrading the signer set's coordination and the node's view of the network (a "propagation of forged/incorrect data" and disruption of the read/consensus-observation path).
- Corruption of the replica's view for readers of that message type: `StackerDBListener` consumes messages per contract and slot and will attempt to deserialize whatever bytes are stored; garbage cross-context bytes stored in that slot replace/occupy the legitimate replicated state until overwritten with a fresh signed message of a higher version.

The impact class matches "network-wide propagation of forged data" / "unauthorized write to StackerDB state" since the node accepts and stores/broadcasts data into a context (contract) the original signer never authorized it for.

### Likelihood Explanation
Likelihood is low-to-moderate, matching the "Medium" risk rating of the original report: it requires the attacker to first observe a validly-signed chunk (trivial — chunks are broadcast/replicated to all StackerDB peers and are also queryable over the public GET chunk RPC), and it requires that the target sibling contract currently accepts a version/size combination that the replayed chunk satisfies. No signer key or privileged role is needed — any p2p peer or RPC client can trigger the replay.

### Recommendation
Bind the target StackerDB context into the signed digest, mirroring the report's fix of folding `questID`/`period` into the merkle leaf. Concretely, include the `QualifiedContractIdentifier` (or equivalently a `StackerDBConfig`/DB identifier hash) into `SlotMetadata::auth_digest()`:
```
hasher.update(smart_contract_id.serialize_to_vec());
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
```
and thread the contract id through `sign`/`verify` call sites in `libstackerdb/src/libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs::try_replace_chunk`, and `stackslib/src/net/stackerdb/mod.rs::validate_received_chunk`. This is a protocol/wire change (all existing signers must sign the new digest), so it needs coordinated rollout, but requires no additional storage.

### Proof of Concept
1. Wait for (or induce) a real signer to publish a validly-signed chunk to `signers-0-1` (`BlockResponse`) for `slot_id = k`, `slot_version = v`, observing `(sig, data)` either via P2P gossip or `GET /v2/stackerdb/{signers-principal}/signers-0-1/{k}`.
2. Confirm via `GET /v2/stackerdb/{signers-principal}/signers-0-2/` metadata that `slot_id = k` is owned by the same signer address in `signers-0-2` (guaranteed by shared `stackerdb-get-signer-slots-page` in `signers.clar`) and that its current `slot_version < v` and `v <= max_writes`.
3. POST the identical `(slot_id=k, slot_version=v, sig, data)` tuple to `/v2/stackerdb/{signers-principal}/signers-0-2/chunks`.
4. Observe `accepted: true` in the `StackerDBChunkAckData` response — the chunk signed for the `BlockResponse` channel is now stored as a `StateMachineUpdate` chunk, and any legitimate `StateMachineUpdate` chunk from the real signer with version `<= v` is subsequently rejected as `StaleChunk`.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L96-108)
```rust
#[derive(Debug, Clone, PartialEq)]
pub enum StackerDBErrorCodes {
    /// The slot already holds a chunk whose version is at least the one submitted.
    DataAlreadyExists,
    /// The chunk's slot ID is out of range for this replica's slot allocation.
    NoSuchSlot,
    /// The chunk's signature does not recover to the address that owns the slot.
    BadSigner,
    /// The chunk exceeds the replica's configured chunk size.
    ChunkTooBig,
    /// The chunk's slot version exceeds the replica's configured maximum writes.
    TooManySlotWrites,
}
```

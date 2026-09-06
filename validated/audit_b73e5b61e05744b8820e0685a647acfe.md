## Title
StackerDB Chunk Signature Lacks Contract/Domain Binding, Enabling Cross-Contract Chunk Replay Across Signer Message Lanes - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the preimage signed to authenticate every StackerDB chunk — commits only to `(slot_id, slot_version, data_hash)`. It omits any identifier of the StackerDB instance (`QualifiedContractIdentifier`) the chunk belongs to. Because the Nakamoto signer set architecture assigns each signer the *same* slot index across multiple independent StackerDB contracts (`signers-0-0` … `signers-0-7`, one per `MessageSlotID` lane), a chunk validly signed for one lane/contract is also a validly-signed chunk for any other lane/contract where that signer occupies the same slot, at any version. This is a direct structural analog of the reported "signed payload omits domain/session fields" bug.

### Finding Description
`SlotMetadata::auth_digest()` is:
```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
``` [1](#0-0) 

`sign()`/`verify()` operate purely on this digest [2](#0-1) . No `QualifiedContractIdentifier`, chain/network ID, or reward-cycle/session value is included in the signed bytes.

At the point of ingestion, `StackerDBSync::validate_received_chunk` authenticates a chunk by looking up the *expected signer address* for `data.slot_id` in the config of the specific `smart_contract_id` being synced, then verifies the chunk's signature against that address:
```rust
let addr = match self.stackerdbs.get_slot_signer(smart_contract_id, data.slot_id)?
{ Some(addr) => addr, None => return Ok(false) };
let slot_metadata = data.get_slot_metadata();
if !slot_metadata.verify(&addr)? { ... return Ok(false); }
``` [3](#0-2) 

Crucially, `smart_contract_id` is used only to select *which address to check against* — it is never mixed into the digest that was actually signed. Whether a chunk verifies depends solely on `(slot_id, slot_version, data_hash)` matching a signature made by the address that owns that slot **in that config**, not on which contract the chunk is destined for.

In the boot contracts, the signer-to-slot mapping is shared across all message lanes for a signer set: `signers-0-{message_id}` and `signers-1-{message_id}` all call back into `.signers` `stackerdb-get-signer-slots-page` for the same page (`0` or `1`), so a given signer occupies the **same slot index** in every one of `signers-0-0` through `signers-0-7` (BlockResponse, MockProposal, MockSignature, MockBlock, StateMachineUpdate, BlockPreCommit, etc.) for a given signer set. [4](#0-3) [5](#0-4) 

Consequently, if a signer S occupies slot N in both `signers-0-1` (BlockResponse) and `signers-0-3` (MockProposal), a chunk `(slot_id=N, slot_version=V, data=D, sig=Sign(N,V,hash(D)))` that S legitimately published to `signers-0-1` is *also* a validly-signed chunk for `signers-0-3`, because `auth_digest` never encoded which contract it was meant for. An attacker who observes this broadcast chunk on the p2p network (StackerDB chunks are gossiped/relayed, not secret) can resubmit the identical `(slot_id, slot_version, sig, data)` tuple to a peer's `signers-0-3` replica via `StackerDBPushChunk`/`PutChunk`. `validate_received_chunk` will look up the slot-N signer for `signers-0-3` (same signer S, since slot assignment is shared), verify the signature against S's address, and — the signature check passes because the digest never bound to the target contract — accept and store/replicate the chunk as canonical data for the wrong message lane and round.

The equality being broken is: "signature verifies for slot N in contract A" is treated as proof of "the signer intended this exact bytes to be data for slot N in contract A", when in fact the signature only proves "the signer produced these bytes for slot N of *some* stackerdb, at *some* version" — the missing binding is exactly the `contract_id`/domain field called out in the external report.

### Impact Explanation
This allows an unauthenticated remote party to cause forged/mismatched data to be accepted and propagated as authentic StackerDB content under the wrong contract (message lane), i.e. serving non-canonical/misattributed data as canonical StackerDB state without holding any private key — the write is unauthorized in the sense that neither the actual slot owner nor the destination contract's operators sanctioned this specific chunk placement. Downstream consumers of a given `signers-0-{message_id}` contract's chunks (e.g., the signer runloop's `StackerDBChunksEvent` -> `SignerEvent` conversion) do apply an additional payload-type/lane check (`signer_message_payload_matches_lane`) before acting on messages, which mitigates some higher-order effects for that specific consumer path [6](#0-5) , but that filter lives only in the signer's own event-ingestion code, not in the core StackerDB storage/replication layer (`stackslib/src/net/stackerdb/mod.rs`, `libstackerdb/src/libstackerdb.rs`). The chunk is still accepted, stored, and propagated network-wide by any node's core StackerDB sync logic before that filter is ever applied, and any other consumer of the raw StackerDB chunk contents that does not implement an equivalent lane-matching check would treat the replayed chunk as authentic for the wrong purpose.

### Likelihood Explanation
Exploitation requires only observing one broadcast StackerDB chunk (chunks are propagated over the p2p network without confidentiality) and replaying it via the standard, unprivileged `StackerDBPushChunk`/`PutChunk` RPC to a different signer-message-lane contract where the signing signer coincidentally (in practice, by protocol design) shares the same slot index. Since slot indices are shared across all `signers-{set}-{message_id}` contracts for a signer set by construction, this precondition holds for essentially every currently-active signer, making the replay trivially and reliably reproducible by any network peer without needing the signer's key.

### Recommendation
Include the target StackerDB contract identity in the signed digest, e.g.:
```rust
fn auth_digest(&self, contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(contract_id.serialize_to_vec());
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
and thread `contract_id` through `sign()`, `verify()`, and every call site (`StackerDBChunkData::sign/verify/recover_pk`, `validate_received_chunk`, `try_replace_chunk`), so a chunk signed for one StackerDB instance can never verify against another, even when the signer/slot mapping happens to coincide.

### Proof of Concept
1. Signer S is registered at slot index `N` for reward-cycle signer set `0`, so S occupies slot `N` in both `signers-0-1` (BlockResponse) and `signers-0-3` (MockProposal) StackerDB contracts (per `stackerdb-get-signer-slots-page` sharing the same page across lanes) [5](#0-4) .
2. S publishes a legitimate `MockProposal` chunk to `signers-0-3`: `StackerDBChunkData { slot_id: N, slot_version: V, data: D }`, signed via `chunk.sign(&S_privkey)` which computes `auth_digest()` over only `(N, V, hash(D))` [7](#0-6) .
3. An unprivileged observer captures this broadcast chunk (`slot_id=N, slot_version=V, sig, data=D`).
4. The observer submits the *identical* tuple as a `StackerDBPushChunk`/`PutChunk` targeting `signers-0-1` (BlockResponse contract) instead.
5. The receiving node's `validate_received_chunk` resolves the slot-`N` signer for `signers-0-1` (also S, since slot mapping is shared), calls `slot_metadata.verify(&S_addr)`, which succeeds because the digest is contract-agnostic [3](#0-2) .
6. The chunk is accepted, stored, and further relayed by the node's StackerDB sync logic as if it were legitimately-produced `BlockResponse` data, even though S never signed it for that contract/lane.

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

**File:** libstackerdb/src/libstackerdb.rs (L223-231)
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

**File:** libsigner/src/events.rs (L580-596)
```rust
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

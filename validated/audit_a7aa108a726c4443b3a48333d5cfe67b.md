### Title
Missing StackerDB/contract binding in `SlotMetadata` signatures allows cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` commits only to `(slot_id, slot_version, data_hash)`. It never binds the signature to the StackerDB's contract identity (or any other domain separator). Because every `signers-{0|1}-{message_id}` StackerDB contract for a given signer-set page shares the exact same slot-index assignment (`stackerdb-get-signer-slots-page`), a signer's validly-signed chunk from one message-type contract is also a validly-signed chunk for the slot with the same index in every other message-type contract of that page. Any unprivileged peer that observes a broadcast chunk can replay the identical `(slot_id, slot_version, sig, data)` tuple into a different StackerDB contract, and the storage layer will accept and propagate it, since neither `try_replace_chunk` nor `validate_received_chunk` binds the verified signature to the contract it is being written into.

### Finding Description
`SlotMetadata::auth_digest` hashes only the slot id, slot version, and data hash: [1](#0-0) 

`SlotMetadata::verify`/`StackerDBChunkData::verify` recover the signer purely from this digest, with no contract/network binding: [2](#0-1) 

The consumers of this signature take a `smart_contract: &QualifiedContractIdentifier` parameter, but they only use it to *look up* the expected signer address — the contract id is never mixed into the signed payload or otherwise checked against the signature itself: [3](#0-2) 

The same pattern exists in the sync/push validation path: [4](#0-3) 

Crucially, the boot contracts assign slot indices identically across all message-id contracts that share a signer-set page — `signers-0-xxx.clar` and `signers-1-xxx.clar` both simply forward to `stackerdb-get-signer-slots-page`, keyed only by page (0 or 1), not by message id: [5](#0-4) [6](#0-5) 

This is confirmed by the test that iterates every `message_id` for a page and observes the identical slot list each time: [7](#0-6) 

Consequently, signer `S` occupies the *same* `slot_id` in `signers-0-0`, `signers-0-1`, ..., `signers-0-{SIGNER_SLOTS_PER_USER-1}` simultaneously. Since the chunk signature never encodes which of these contracts it was produced for, a chunk `(slot_id=k, slot_version=v, sig=σ, data=D)` that `S` legitimately signed and pushed to contract `signers-0-1` (e.g. a `BlockResponse` message) is *also* a validly-signed chunk for slot `k` in contract `signers-0-2` (e.g. the `BlockProposal`/other-lane contract), as long as the target slot's current version is `< v`. Any peer that has seen the original broadcast (no private key required) can resubmit the exact same bytes to the sibling contract via `POST /v2/stackerdb/.../chunks` or via unsolicited `StackerDBPushChunkData`, and the storage layer (`try_replace_chunk` / `validate_received_chunk`) will accept it purely because `slot_desc.verify(&slot_validation.signer)` succeeds — it has no way to know the signature was minted for a different contract.

This mirrors the reported bug class exactly: a signature valid in one context (chain id A / contract A) is accepted in another context (chain id B / contract B) because the context identifier was never included in what was signed.

### Impact Explanation
An unprivileged network peer can cause a node to store and further gossip (via the StackerDB replication protocol) data into a slot of a StackerDB contract it was never authorized for — an unauthorized write to StackerDB state and network-wide propagation of misattributed/stale data, satisfying the Critical bar ("unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data"). Concretely, this can be used to pollute another signer-message lane (e.g. writing a `BlockProposal`-flavored chunk into the slot that other signers/miners expect to hold `BlockResponse` data for the same signer), and, more generally, once the same signer/slot pairing recurs (e.g. same signer retains the same sorted position across further deployed message-id contracts), any previously observed valid chunk becomes replayable across all of them without the signer's cooperation.

### Likelihood Explanation
Exploitation requires only: (1) passively observing one broadcast, signed StackerDB chunk (trivial — StackerDB chunks are gossiped to all replicating peers), and (2) resubmitting the identical bytes to a sibling `signers-{page}-{other_message_id}` contract with a currently-lower slot version (also trivial, since these contracts' fresh slots typically start at version 0/`NO_VERSION`). No node secret, signer key, or admin role is needed — only the ability to relay traffic that any p2p/RPC peer already has.

### Recommendation
Bind the StackerDB contract identity (and, ideally, a network/chain identifier) into the signed digest, analogous to how `Claimable`/`PhiFactory` should re-derive `block.chainid` before verifying. Concretely, extend `SlotMetadata::auth_digest` (and `StackerDBChunkData::sign`/`verify`) to include the `QualifiedContractIdentifier` of the target StackerDB (or a stable hash of it) as part of the hashed preimage, and thread that same contract id into `try_replace_chunk`/`validate_received_chunk` so a chunk signed for one StackerDB contract cannot verify against another.

### Proof of Concept
1. Set up two StackerDB contracts sharing the same signer-set page, e.g. `signers-0-0` and `signers-0-1`, each with signer `S` owning `slot_id = k` (guaranteed identical ordering per `stackerdb-get-signer-slots-page`).
2. Have `S` sign and push a chunk `StackerDBChunkData { slot_id: k, slot_version: 1, sig, data }` to `signers-0-0`, using `StackerDBChunkData::sign` (as in `SlotMetadata::sign`/`verify`, `libstackerdb/src/libstackerdb.rs:171-193`).
3. As a third, unprivileged party who merely observed this chunk (e.g. from a `poststackerdbchunk` broadcast or `StackerDBPushChunkData`), resend the exact same `(slot_id, slot_version, sig, data)` to `signers-0-1`'s replica via `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-437`) or `handle_unsolicited_StackerDBPushChunk`.
4. Observe that `slot_desc.verify(&slot_validation.signer)` (`db.rs:418`) succeeds because the digest never encoded which contract the signature belonged to, and the chunk is accepted into `signers-0-1`'s slot `k` even though `S` never signed anything for that contract.

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

**File:** stackslib/src/net/stackerdb/db.rs (L398-423)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
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

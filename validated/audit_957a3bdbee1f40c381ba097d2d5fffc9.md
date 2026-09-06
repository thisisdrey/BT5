### Title
StackerDB chunk signatures omit the contract identifier, enabling cross-database chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` — the digest that a `StackerDBChunkData` signature actually commits to — only covers `slot_id`, `slot_version`, and `data_hash`. It never binds the signature to the specific StackerDB smart contract the chunk was written for. Because the Stacks signer set (`.signers-{0,1}-{page}` contracts) intentionally assigns the *same* `slot_id` to a given signer across all of that reward cycle's per-message-type StackerDB instances, a chunk that a signer validly signs and broadcasts for one contract (e.g. `signers-1-0`) is a byte-for-byte valid, verifiable chunk for any other contract in which that signer occupies the same slot (e.g. `signers-1-3`). Any unprivileged network participant can therefore capture a legitimately signed chunk and resubmit/relay it into a different StackerDB contract, where it will pass signature verification and be stored/propagated as if it were authentic content of that database.

### Finding Description
The signing/verification logic lives in `libstackerdb/src/libstackerdb.rs`: [1](#0-0) 

`auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`; there is no field identifying which StackerDB (i.e., which `QualifiedContractIdentifier`) the signature is meant for. `StackerDBChunkData::sign`/`verify` simply delegate to this digest.

Storage-side validation reproduces the same gap. `StackerDBTx::try_replace_chunk` looks up the expected signer for `smart_contract`/`slot_id` and checks `slot_desc.verify(&slot_validation.signer)`, but the verified digest itself carries no reference to `smart_contract`: [2](#0-1) 

The network-facing gossip/push path does the same thing — it resolves the expected signer for the *target* contract and slot, then verifies the chunk's self-contained signature against it: [3](#0-2) 

Critically, the signer-to-slot assignment is deliberately shared across all of a reward cycle's per-message-type signer contracts. The boot contract stores one signer-slot list per reward-cycle parity and every `.signers-{cycle_mod}-{page}` contract reads from that same list: [4](#0-3) 

This is confirmed by the test showing identical `stackerdb-get-signer-slots` output for every `message_id` page under a given `signer_set`: [5](#0-4) 

Consequently, a signer's `slot_id` is identical across contracts `signers-{cycle_mod}-0`, `signers-{cycle_mod}-1`, `signers-{cycle_mod}-2`, ... (one per `MessageSlotID`, e.g. `BlockResponse`, `Transactions`, `StateMachineUpdate`). Since the chunk signature never binds to the contract, a valid chunk broadcast under one `MessageSlotID` contract can be replayed verbatim into a sibling contract for the same signer/slot, and it will pass every check in `try_replace_chunk`/`validate_received_chunk` (signer match, version freshness, size, write-count) purely because those checks re-derive the *expected* signer from the *target* contract rather than from anything the signature itself attests to.

### Impact Explanation
This breaks the equality "a chunk stored/propagated by a StackerDB instance was actually signed for that instance." An unprivileged network peer that observes (or is sent) any legitimately signed chunk can re-POST it (`POST /v2/stackerdb/{addr}/{name}/chunks`) or re-relay it via `StackerDBPushChunk` gossip into a different sibling StackerDB contract that shares the same signer/slot assignment, causing the replica to accept and store/gossip data that was never intended for, and never authorized by the signer, in that database context. This is an unauthorized write into StackerDB state and network-wide propagation of misattributed/forged-context data (consumers that deserialize per-contract, e.g. `StackerDBListener`/`SignerMessageV0` state-machine consumers, will treat it as authentic signer output for that channel), matching the High/Critical class of "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data."

### Likelihood Explanation
Exploitation requires no secret key and no privileged role — only observing one broadcast chunk (chunks are gossiped in the clear and are also fetchable via the read StackerDB HTTP API) and resubmitting it against a different, sibling contract name. The precondition (shared slot assignment across a reward cycle's message-type contracts) is not incidental — it is the intended, hard-coded design of `signers.clar`/`SIGNER_SLOTS_PER_USER`, so the precondition holds for essentially every reward cycle and every signer, making this reliably and repeatedly reachable by any peer with network access to a node's HTTP/p2p endpoints.

### Recommendation
Include the StackerDB's contract identifier (e.g. `QualifiedContractIdentifier`) as part of the signed digest in `SlotMetadata::auth_digest` (and thus in `StackerDBChunkData::sign`/`verify`), so a signature is cryptographically bound to the specific StackerDB instance it authorizes. Verification code in `StackerDBTx::try_replace_chunk` and `PeerNetwork::validate_received_chunk` should pass the contract id into signature verification instead of relying solely on the caller-supplied "expected signer" lookup being contract-specific.

### Proof of Concept
1. Reward cycle `N` (parity `N % 2`) has signer `S` with private key `sk` assigned `slot_id = k` in both `.signers-{N%2}-0` (e.g., `BlockResponse`) and `.signers-{N%2}-3` (e.g., `Transactions`), per the shared slot list in `signers.clar`.
2. `S` legitimately signs and pushes chunk `C = StackerDBChunkData{slot_id:k, slot_version:v, sig, data}` to `.signers-{N%2}-0`, and it propagates over gossip / is fetchable via the read endpoint.
3. An unrelated peer captures `C` and re-POSTs it unmodified to `.signers-{N%2}-3`'s chunk endpoint (or relays it via `StackerDBPushChunk` addressed to that contract).
4. `validate_received_chunk`/`try_replace_chunk` look up the expected signer for `.signers-{N%2}-3`/slot `k`, which is also `S`; `slot_desc.verify(&S)` succeeds because the digest never referenced which contract it was for. If `v` exceeds the currently stored version in `.signers-{N%2}-3` and is within `max_writes`, the replica accepts and stores/gossips `C` as authentic content of `.signers-{N%2}-3`, even though `S` never signed anything for that contract.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-193)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
```rust
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L15-43)
```text
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

;; Called internally by the Stacks node.
;; Sets the list of signers and weights for a given reward cycle.
(define-private (set-signers
                 (reward-cycle uint)
                 (signers (list 4000 { signer: principal, weight: uint })))
     (begin
      (asserts! (is-eq (var-get last-set-cycle) reward-cycle) (err ERR_CYCLE_NOT_SET))
      (ok (map-set cycle-signer-set reward-cycle signers))))

;; Get the list of signers and weights for a given reward cycle.
(define-read-only (get-signers (cycle uint))
     (map-get? cycle-signer-set cycle))

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

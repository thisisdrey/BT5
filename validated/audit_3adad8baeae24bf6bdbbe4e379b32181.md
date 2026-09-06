### Title
Cross-contract StackerDB chunk replay due to missing contract-domain separation in `SlotMetadata` signature - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The signature that authenticates a StackerDB chunk (`SlotMetadata::auth_digest`) is computed only over `slot_id`, `slot_version`, and `data_hash`. It never binds the digest to the StackerDB smart-contract identifier the chunk is destined for. Because several distinct StackerDB replicas (e.g. the per-message-type `signers-<set>-<message_id>` contracts) are configured with the exact same signer→slot_id assignment for a given reward cycle, a signature that is valid for slot `N` in one contract is *also* valid for slot `N` in a sibling contract. An unprivileged relayer who observes one validly-signed chunk broadcast for contract A can therefore replay/re-target the identical wire bytes to contract B, and the receiving node's chunk-acceptance logic (`try_replace_chunk` / `validate_received_chunk`) will accept and store it as if it were an authentic write to contract B, since the check only re-derives the signer from that contract's own slot table and never confirms that the signature was scoped to that contract.

### Finding Description
`SlotMetadata::auth_digest` in `libstackerdb/src/libstackerdb.rs` computes the signed digest as: [1](#0-0) 
i.e. `SHA512/256(slot_id || slot_version || data_hash)`. There is no contract identifier, message-type discriminator, or any other domain separator mixed into this digest. `verify()` simply recovers a public key from this digest and the supplied signature and checks it against the expected principal: [2](#0-1) 

Chunk acceptance paths use exactly this digest/verify pair, scoped only by looking up the *expected signer* for the *target contract's* slot:

- HTTP write path (`stackslib/src/net/api/poststackerdbchunk.rs`) resolves the slot's owner from the target contract and then calls `try_replace_chunk`, which does `slot_desc.verify(&slot_validation.signer)`: [3](#0-2) 

- P2P push/gossip path `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs` does the analogous thing — it fetches the signer address *for the target contract's slot* and verifies the digest against it, again with no binding to which contract the signature was meant for: [4](#0-3) 

The signer→slot_id assignment is not unique per-contract. For the `.signers-<set>-<message_id>` family of StackerDB contracts, the slot list is read straight from the shared `.signers` contract via `stackerdb-get-signer-slots-page`, keyed only by the reward-cycle-parity (`page`), not by message_id: [5](#0-4) [6](#0-5) 

This is confirmed by the test `signers_db_get_slots`, which shows that every `signers-<set>-<message_id>` contract for a given signer set returns the *identical* `(signer, num-slots)` list, and thus the identical slot_id→signer mapping, across all message-type contracts sharing that set: [7](#0-6) 

Because the digest that gets signed contains no contract-specific salt, a `StackerDBChunkData{slot_id, slot_version, sig, data}` that a signer validly produced and broadcast for contract A (e.g. `signers-1-0`, used for one message type) is byte-for-byte a valid, verifiable chunk for the sibling contract B (e.g. `signers-1-1`, used for a different message type), for the same slot_id, as long as B's per-slot version/staleness/write-count checks in `try_replace_chunk`/`validate_received_chunk` are independently satisfiable (each contract tracks its own `slot_version` state, so an attacker simply needs to pick a `slot_version` that is fresh for B — trivial, since B's version state starts independently from A's).

### Impact Explanation
This breaks the intended equality "a signature over a chunk authenticates that chunk *for the specific replicated database it was written to*." An unprivileged network participant (anyone who can observe a broadcast StackerDB chunk, e.g. via P2P gossip or the public write RPC) can re-post that exact chunk to a different StackerDB contract that happens to share the same slot assignment, causing the receiving node to store forged, out-of-context data under a legitimate signer's identity in a StackerDB replica that signer never wrote to. Given that StackerDB slots for signer messages carry semantically distinct payloads per message type (block responses, transactions, state-machine updates, etc.), successful cross-posting corrupts a different logical channel with data purporting to come from that signer, and any consumer of that channel that trusts "signature verified ⇒ authored by signer for this DB" is misled — this is a forged-data propagation / unauthorized write into StackerDB state, matching the Critical-impact bucket ("unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data").

### Likelihood Explanation
No secret key or privileged role is required — the attacker only needs a previously-observed, validly-signed chunk (trivially obtainable since StackerDB chunks are broadcast/replicated openly) and network access to push/POST it to a sibling StackerDB replica. The only constraint is that the two contracts must assign the same slot_id to the same signer, which the `.signers`/`signers-<set>-<message_id>` boot-contract design guarantees by construction for every message-type contract within a signer set. This makes the precondition for exploitation deterministic and always true for the signer-messaging StackerDBs, rather than a rare coincidence.

### Recommendation
Bind the signed digest to the target StackerDB contract (and, ideally, to the message-type/slot-purpose) so that a signature cannot be replayed across sibling replicas. Concretely, include the `QualifiedContractIdentifier` (or a stable hash of it) as part of `SlotMetadata::auth_digest` in `libstackerdb/src/libstackerdb.rs`, and thread the contract id into `sign`/`verify` call sites (`stackslib/src/net/stackerdb/db.rs::try_replace_chunk`, `stackslib/src/net/stackerdb/mod.rs::validate_received_chunk`, and the HTTP handler in `stackslib/src/net/api/poststackerdbchunk.rs`). This is a wire/consensus-adjacent format change and needs careful versioning/rollout since it changes what bytes get signed.

### Proof of Concept
1. Two StackerDB contracts, `signers-1-0` and `signers-1-1`, are active for the same reward cycle and (per `signers_db_get_slots`/`signers.clar`) assign slot 5 to the same signer address `S`.
2. Signer `S` legitimately signs and pushes chunk `C = {slot_id: 5, slot_version: 7, sig, data: D}` to `signers-1-0`. This chunk is gossiped over P2P and observable by any peer.
3. Attacker (no keys required) captures `C` and re-submits the identical `slot_id/slot_version/sig/data` tuple as a chunk write for `signers-1-1` (via HTTP `POST /v2/stackerdb/{principal}/{contract_name}/chunks`, matching `poststackerdbchunk.rs`, or via the P2P push handler).
4. `try_replace_chunk`/`validate_received_chunk` for `signers-1-1` looks up its own slot-5 signer (`S`, same as in `signers-1-0`), recomputes `auth_digest()` from `(5, 7, hash(D))` — identical to what was signed for `signers-1-0` — and `slot_desc.verify(&S)` succeeds because the digest formula ignores the contract identifier.
5. Provided `signers-1-1`'s slot 5 is currently at a version < 7 (independently tracked per contract, so trivially true on a fresh/low-activity contract), the write is accepted and stored, ack'd as `accepted: true`, and gossiped further — forged data now lives in `signers-1-1` attributed to signer `S`, who never wrote it there.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-1-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 1.
;; The contract name indicates which -- it has the form `signers-1-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u1))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-340)
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
```

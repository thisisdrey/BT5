### Title
StackerDB chunk signatures omit the destination contract (lane) identity, allowing cross-contract chunk replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs` signs only `(slot_id, slot_version, data_hash)`. It never binds the signature to the `QualifiedContractIdentifier` of the StackerDB it is destined for. Because the `.signers-<set>-<message_id>` boot contracts (`BlockResponse`, `StateMachineUpdate`, `BlockPreCommit`) all share the *same* signer→slot mapping for a given signer set, the same address occupies the same `slot_id` across multiple, independent StackerDB replicas. A chunk that a signer legitimately signs and pushes to one lane (e.g. `signers-1-2`, StateMachineUpdate) carries a signature that is *also* valid for another lane (e.g. `signers-1-1`, BlockResponse, or `signers-1-3`, BlockPreCommit), since none of these values differ between contracts. This is the same bug class as the reported "authentication tokens reused across chains" issue: the authenticator commits to too little context (missing the "destination contract" analogue of chain ID), so a valid credential minted for one destination can be replayed against another.

### Finding Description
`SlotMetadata::auth_digest()` computes:
```
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
``` [1](#0-0) 
and `sign`/`verify` operate purely on this digest and a `StacksAddress`: [2](#0-1) 

The node-side acceptance check, `StackerDBs::validate_received_chunk`, resolves the expected signer *only* by looking up `smart_contract_id`/`slot_id` in the DB (`get_slot_signer`), then calls `slot_metadata.verify(&addr)`, which re-derives the identical, contract-agnostic digest: [3](#0-2) 
This function is shared by both the pull path (`StackerDBSync::validate_downloaded_chunk`) and the unsolicited push path (`PeerNetwork::handle_unsolicited_StackerDBPushChunk`), so it gates every network-supplied chunk write, and none of these paths reintroduce a contract-id or lane binding before calling `verify`.

The signer-slot list is derived from a single shared source, `.signers` contract's `stackerdb-get-signer-slots-page`, which is called identically by every message-id contract for a signer set (`signers-<set>-1`, `signers-<set>-2`, `signers-<set>-3`): [4](#0-3) [5](#0-4) 
The in-repo test `signers_db_get_slots` explicitly confirms that all three `signers-<set>-{1,2,3}` contracts return the *same* slot list for a given signer set: [6](#0-5) 

Consequently: given a `StackerDBChunkData{slot_id: k, slot_version: v, sig, data}` legitimately signed by signer `S` and observed being propagated to contract `signers-1-2` (StateMachineUpdate), an unprivileged network observer can resubmit the *exact same bytes* (unchanged `sig`, `slot_id`, `slot_version`, `data`) as a chunk destined for `signers-1-1` (BlockResponse) or `signers-1-3` (BlockPreCommit), for the same signer set. Because `S` occupies slot `k` in all three contracts, `get_slot_signer` returns `S` there too, and `slot_metadata.verify(&S)` succeeds — the digest never encoded which contract it was meant for. Version-staleness (`data.slot_version < *expected_version`) and write-count (`max_writes`) checks are per-contract counters and are trivially satisfiable against a fresh/behind replica.

### Impact Explanation
This is an unauthenticated/unauthorized write into a StackerDB replica that the DB write-authorization logic is supposed to gate by signer identity and (implicitly) intended destination. It:
- Lets any network observer graft a signer's signature onto an unrelated StackerDB lane, forging state that appears to have been authored by that signer for a contract they never intended to write to, and this state is propagated network-wide via StackerDB chunk push/gossip and sync (`handle_unsolicited_StackerDBPushChunk`/`StackerDBSync`) — i.e., unauthenticated/unauthorized write to StackerDB with network-wide propagation of forged (mis-attributed) data, per the Critical bucket.
- Consumes the target lane's independent version/write-count state (`slot_version`, `max_writes`) under the victim signer's identity without their consent, degrading the availability/integrity of that signer's future legitimate writes to that lane.
Downstream application-level consumers (e.g. `SignerEvent::TryFrom<StackerDBChunksEvent>` in `libsigner/src/events.rs`) do filter by payload-type-prefix against the expected lane, which limits — but does not eliminate — the blast radius to storage-layer pollution/version-griefing rather than semantic misinterpretation of the payload as a valid vote in the wrong lane. Because the DB-write acceptance path itself has no such filtering, the write is accepted and disseminated before any type-aware filtering ever occurs.

### Likelihood Explanation
High feasibility: StackerDB chunk data and signatures are broadcast in the clear over the P2P network and via HTTP StackerDB endpoints; no secret is required to replay them. The three sibling message-id contracts for a signer set are boot contracts with fixed, predictable names (`signers-<set>-1/2/3`), and their identical slot-assignment is enforced by design (confirmed by `signers_db_get_slots` test). An attacker only needs to observe one legitimately signed chunk to replay it elsewhere; no cryptographic break or privileged access is needed.

### Recommendation
Include the destination `QualifiedContractIdentifier` (and ideally the network/chain identifier and, for the P2P-message analog, the message-specific context already used elsewhere in the codebase, e.g. as done for `BlockRejection.chain_id`/domain-separated hashing in `libsigner/src/v0/messages.rs`) inside `SlotMetadata::auth_digest()`, so that a chunk signature is only valid for the specific StackerDB contract it was created for. Update `sign`/`verify`/`recover_pk` call sites accordingly and add a contract-id parameter through `StackerDBChunkData` handling, then bump the wire/DB schema/version as needed.

### Proof of Concept
1. Signer `S` legitimately signs and pushes a `StackerDBChunkData{slot_id: 3, slot_version: 5, data: D}` to `signers-1-2` (StateMachineUpdate lane), where `sig = Sign(S, hash(3 || 5 || Hash(D)))`.
2. An attacker observes this chunk on the wire (P2P push/gossip or via the StackerDB HTTP GET endpoint).
3. The attacker submits an identical `StackerDBChunkData{slot_id: 3, slot_version: 5 (or any value ≥ current version in the target replica), sig, data: D}` to `signers-1-1` (BlockResponse lane) or `signers-1-3` (BlockPreCommit lane), where `S` also occupies slot 3 (guaranteed by shared `stackerdb-get-signer-slots-page`).
4. `validate_received_chunk` resolves `get_slot_signer(signers-1-1, 3) == S`, recomputes the same contract-agnostic `auth_digest`, and `slot_metadata.verify(&S)` returns `true` — the chunk is accepted and stored/propagated in `signers-1-1`, even though `S` never signed anything intended for that lane.

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

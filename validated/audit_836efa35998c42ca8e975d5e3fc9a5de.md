### Title
StackerDB chunk signature omits the contract/StackerDB identifier, allowing cross-StackerDB chunk replay (message-type confusion) - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The external advisory describes a JWT "type confusion" where a token signed for one purpose (a Verifiable Presentation) is accepted as valid for a different purpose (an access token) because the signature/claims never bind the token to its intended type or issuer context. The exact same class of bug exists in this repo's StackerDB chunk-authentication scheme: a chunk's signature is computed only over `(slot_id, slot_version, data_hash)` and never binds to the specific StackerDB (smart contract) it was written to. Because Nakamoto's per-cycle signer StackerDBs (`signers-{0|1}-{message_id}`) all share the *identical* signer→slot-id assignment within a signer set, a chunk validly signed by signer `S` for slot `K` in one message-purpose StackerDB (e.g. `signers-0-1`, used for one message type) is *also* a validly verifiable chunk for slot `K` in every other same-signer-set StackerDB (e.g. `signers-0-2`, used for a different message type), since verification never checks which contract the signature was intended for.

### Finding Description
`SlotMetadata::auth_digest()` computes the signed digest as: [1](#0-0) 
i.e. `SHA512/256(slot_id || slot_version || data_hash)`. Verification, `SlotMetadata::verify()`, recovers the public key from this same digest and checks it hashes to the expected `principal`: [2](#0-1) 

Neither the digest nor the verification takes the StackerDB's smart-contract identifier (the "context"/"purpose", analogous to a JWT `typ`/`iss`) into account.

The write path, `StackerDBs::try_replace_chunk`, receives the target `smart_contract` as a parameter, looks up the expected signer *for that contract and slot*, and calls `slot_desc.verify(&slot_validation.signer)` — again with no cross-check that the signature was produced *for this contract*: [3](#0-2) 

The same pattern is repeated in the P2P gossip acceptance path, `PeerNetwork::validate_received_chunk`, which resolves the expected signer address purely from `(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)`: [4](#0-3) 

Critically, in Nakamoto, the signer set's slot assignment is shared verbatim across *all* per-message-type StackerDB contracts within a signer set. The `.signers` boot contract stores a single list per signer set (`stackerdb-signer-slots-0` / `-1`), and every `signers-{set}-{message_id}` contract simply proxies to the same page: [5](#0-4) [6](#0-5) 

This is confirmed by the test that iterates `message_id in 0..SIGNER_SLOTS_PER_USER` and shows every `signers-{set}-{message_id}` contract returns the identical slot list for a given signer set: [7](#0-6) 

Consequently, for a given signer set (e.g. cycle-mod-0), signer `S` owning slot `K` in `signers-0-1` also owns slot `K` in `signers-0-2`, `signers-0-3`, etc. Since the chunk signature is over `(slot_id, slot_version, data_hash)` only — with no domain separator for the contract/message-type — any chunk `S` legitimately posts to `signers-0-1` (publicly observable via gossip or the HTTP `GET chunk` endpoint) is a byte-for-byte valid, signature-passing chunk for the same slot in `signers-0-2` (or any other `signers-0-*` DB), as long as the target slot's current version is lower than the replayed chunk's version (trivial, since `max-writes` is effectively unbounded — `MAX_WRITES = u4294967295` — and `write-freq = 0`): [8](#0-7) 

This is the direct structural analog of the reported issue: the introspection endpoint accepted any JWT signed by a known key without checking that the JWT's `typ`/`iss` bound it to the intended purpose; here, the StackerDB write/relay path accepts any chunk signed by a known slot-owner key without checking that the signature was produced for the specific StackerDB (message-type) it is being written into.

### Impact Explanation
An attacker who observes any chunk broadcast/replicated for one signer-message-type StackerDB (chunks are not confidential — they are gossiped and readable via `GET /v2/stackerdb/{contract}/{slot}/chunk`) can replay it, unmodified, into a *different* same-signer-set StackerDB contract that is dedicated to a different message type. This is an unauthenticated write of attacker-selected (replayed) content into StackerDB state that other participants (miners, other signers) will treat as authentic, freshly-signed data for the wrong message pool — i.e., non-canonical/misattributed data served as canonical for a context it was never intended for. Depending on which message pools are targeted, this could pollute or desynchronize signer coordination state read by consumers that trust "this slot in this StackerDB belongs to signer S" without further contextual validation at the application layer (e.g., stale `Transactions`/`BlockResponse`/mock-signing payloads reappearing in an unrelated message pool), matching the "High: non-canonical state served as canonical" impact bucket defined by the scan rules.

### Likelihood Explanation
The prerequisites are easy for a remote, unprivileged attacker to satisfy: StackerDB chunks are gossiped/publicly readable by design (no signer key needed), and the write endpoint (`POST /v2/stackerdb/{contract}/chunks`, `poststackerdbchunk.rs`) and the P2P push path both perform only the signature/slot checks shown above — no contract-binding check exists anywhere in the verification chain. The only constraints are the target slot's chunk-size limit and a monotonically-increasing version number, both trivially satisfiable given `max-writes = u32::MAX` and `write-freq = 0` for signer StackerDBs.

### Recommendation
Bind the chunk signature to the StackerDB's smart-contract identifier (and ideally the expected message/purpose), e.g. include `smart_contract.serialize_to_vec()` (or a stable contract hash) in `SlotMetadata::auth_digest()`, and verify it in `SlotMetadata::verify()`. This mirrors the nuts-node fix of binding `iss` to `kid` and enforcing a `typ` claim: add a "must-be-for-this-DB" check before accepting a chunk in `StackerDBs::try_replace_chunk` and `PeerNetwork::validate_received_chunk`.

### Proof of Concept
1. Signer `S` is assigned slot `K=5` in both `signers-0-1` and `signers-0-2` (guaranteed, since both derive from `stackerdb-signer-slots-0`).
2. `S` legitimately posts chunk `(slot_id=5, slot_version=3, data=D)` to `signers-0-1`; the chunk (with `S`'s valid signature) is retrievable via `GET /v2/stackerdb/{signers-0-1-contract}/5/chunk`.
3. Attacker fetches this chunk and re-POSTs the identical `(slot_id=5, slot_version=3, data=D, sig=S's sig)` to `signers-0-2` via `POST /v2/stackerdb/{signers-0-2-contract}/chunks` (or injects it as a `StackerDBPushChunk` P2P message).
4. `try_replace_chunk`/`validate_received_chunk` for `signers-0-2` looks up its own `slot_validation.signer` for slot 5 (also `S`, per the shared slot list), calls `slot_desc.verify(&S)`, which succeeds because the digest never referenced `signers-0-1` vs `signers-0-2`.
5. The chunk is accepted and stored in `signers-0-2`'s slot 5 even though `S` never signed anything intending it for that message pool.

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L1-43)
```text
(define-data-var last-set-cycle uint u0)
(define-data-var stackerdb-signer-slots-0 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-data-var stackerdb-signer-slots-1 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-map cycle-set-height uint uint)
(define-constant MAX_WRITES u4294967295)
(define-constant CHUNK_SIZE (* u2 u1024 u1024))
(define-constant ERR_NO_SUCH_PAGE u1)
(define-constant ERR_CYCLE_NOT_SET u2)

(define-map cycle-signer-set uint (list 4000 { signer: principal, weight: uint }))

;; Called internally by the Stacks node.
;; Stores the stackerdb signer slots for a given reward cycle.
;; Since there is one stackerdb per signer message, the `num-slots` field will always be u1.
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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

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

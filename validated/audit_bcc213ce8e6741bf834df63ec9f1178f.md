### Title
StackerDB chunk signatures omit the target contract, enabling cross-StackerDB chunk replay across `.signers-N-*` message-type replicas - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest` (and thus every `StackerDBChunkData` signature) only commits to `slot_id`, `slot_version`, and `data_hash`. It never commits to the target StackerDB's smart-contract identifier. Because the Stacks signer-set boot contracts (`signers-0-{msg_id}` / `signers-1-{msg_id}`) all derive their slot assignment from the exact same underlying signer list (`stackerdb-get-signer-slots-page`), a given signer occupies the identical `slot_id` in every message-type StackerDB for its signer set. A chunk validly signed for one message-type StackerDB therefore also verifies as valid for any other message-type StackerDB in the same signer set, letting a chunk be replayed/relayed into the wrong StackerDB "audience."

### Finding Description
`SlotMetadata::auth_digest` computes the signed hash as `sha512/256(slot_id || slot_version || data_hash)` with no reference to which StackerDB (smart contract) the chunk belongs to: [1](#0-0) 

`SlotMetadata::verify` / `StackerDBChunkData::verify` only check this digest against a supplied `principal` address — they take no contract context either: [2](#0-1) 

On the storage side, `StackerDBTx::try_replace_chunk` looks up the slot owner *for the specific `smart_contract`* and then calls `slot_desc.verify(&slot_validation.signer)` — the equality it enforces is "signature recovers to the address recorded as owner of `slot_id` in `smart_contract`," but the signature payload itself carries no `smart_contract` binding, so the same signature satisfies this check for *any* contract where that signer owns the same `slot_id`: [3](#0-2) 

The same gap exists in the network-side validator used for both downloaded and pushed chunks, `validate_received_chunk`, which resolves the owning address via `get_slot_signer(smart_contract_id, slot_id)` and again verifies only against that address, not the contract: [4](#0-3) 

The signer-set boot contracts guarantee slot-id collisions across contracts by design: every `signers-0-{msg_id}` contract (one per signer message type) fetches the *same* list from `.signers`: [5](#0-4) 

and `.signers` stores just one list per cycle-parity, shared by every message-type contract that queries page 0 or page 1: [6](#0-5) 

Because slot indices are assigned by simple list position when a StackerDB is (re)configured from this list, a given signer's address ends up at the *same* `slot_id` in every message-type StackerDB for that signer-set/parity: [7](#0-6) 

Consequently: a chunk `(slot_id, slot_version, data, sig)` that signer S legitimately produced and broadcast for StackerDB contract `signers-0-A` (e.g. a "block-response" message DB) has a signature that will also `verify()` successfully against the identical `slot_id` owner in a *different* StackerDB contract `signers-0-B` (e.g. a "transactions" message DB), since the signed digest never encodes which contract it targets. Any peer that already has that publicly-broadcast chunk (chunks are freely downloadable by any StackerDB-replicating peer) can submit/relay it as if it were data for the other contract, as long as that other contract's freshness (`slot_version`) and `max_writes` checks pass.

This mirrors the reported bug class: an authorization artifact (JWT audience / here, a chunk signature) is not bound to the specific resource/target it was minted for (OAuth resource / here, the target StackerDB contract), so the "authenticated for A" fact is incorrectly accepted as "authenticated for B."

### Impact Explanation
This breaks the equality "signature valid for slot X in contract A" vs. "signature valid for slot X in contract B, since same signer owns both slots" that the system implicitly assumes are distinct security boundaries. A remote, unprivileged peer (no private key needed — only relaying already-public signed bytes) can pollute or overwrite a StackerDB replica that the same signer legitimately writes to under a *different* purpose/contract, causing consumers of that StackerDB (miners, other signers, block-proposal machinery) to ingest chunk bytes that were never intended for that channel. Depending on how strictly each consumer parses/serializes messages by slot/contract, this can propagate forged (mis-attributed) data across the network — an unauthenticated write into state that should have required contract-specific authorization, matching a High/Critical class per the rules (network-wide propagation of data accepted as authentic for a channel it was never authorized for).

### Likelihood Explanation
The precondition — the same signer occupying the same `slot_id` across multiple `signers-{0,1}-{msg_id}` StackerDB contracts — is guaranteed by protocol design (all message-type contracts for the same set/parity read the identical slot list). Chunks are broadcast/replicated openly to any peer following that StackerDB, so obtaining a legitimately-signed chunk to replay requires no special access. The remaining constraints (matching `slot_version`/freshness and `max_writes` on the target contract) are ordinary, easily satisfiable conditions, not an attacker-controlled secret.

### Recommendation
Bind the target StackerDB identity into the signed digest, e.g. include the `smart_contract` `QualifiedContractIdentifier` (or a stable numeric/string StackerDB id) as an additional field hashed into `SlotMetadata::auth_digest`, and require `verify()`/`try_replace_chunk`/`validate_received_chunk` to pass and check that identifier explicitly. This is a wire-format/signing change and needs coordinated rollout (versioning or epoch-gating), similar in spirit to how the OAuth advisory's fix records and enforces the bound resource at each check.

### Proof of Concept
1. Let signer `S` own `slot_id = 5` in both `signers-0-3` (msg type "block response") and `signers-0-7` (msg type "transactions") — guaranteed since both contracts read the same `stackerdb-get-signer-slots-page u0` list.
2. `S` signs and legitimately publishes chunk `C = (slot_id=5, slot_version=10, data=D, sig=Sig)` to `signers-0-3`. Any peer following `signers-0-3` can fetch `C` (StackerDB replication is public).
3. An attacker peer takes `C` unmodified and submits/pushes it as a chunk for `signers-0-7`, using the same `slot_id=5`.
4. `try_replace_chunk`/`validate_received_chunk` for `signers-0-7` look up slot 5's owner (`S`), call `slot_desc.verify(&S)`, which succeeds because `auth_digest` only covers `(slot_id, slot_version, data_hash)` — none of which differ between the two contracts. The chunk is accepted into `signers-0-7`'s replica even though `S` never signed data intended for that message channel.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L1-24)
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
```

**File:** stackslib/src/net/stackerdb/config.rs (L205-277)
```rust
    fn eval_signer_slots(
        chainstate: &mut StacksChainState,
        burn_dbconn: &dyn BurnStateDB,
        contract_id: &QualifiedContractIdentifier,
        tip: &StacksBlockId,
    ) -> Result<Vec<(StacksAddress, u32)>, NetError> {
        let value = chainstate.eval_read_only(
            burn_dbconn,
            tip,
            contract_id,
            &format!("({STACKERDB_SLOTS_FUNCTION})"),
        )?;

        let result = value.expect_result()?;
        let slot_list = match result {
            Err(err_val) => {
                let err_code = err_val.expect_u128()?;
                let reason = format!(
                    "Contract {} failed to run `stackerdb-get-signer-slots`: error u{}",
                    contract_id, &err_code
                );
                warn!("{}", &reason);
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }
            Ok(ok_val) => ok_val.expect_list()?,
        };

        let mut total_num_slots = 0u32;
        let mut ret = vec![];
        for slot_value in slot_list.into_iter() {
            let (addr, num_slots) =
                Self::parse_slot_entry(slot_value, contract_id).map_err(|e| {
                    warn!("Failed to parse StackerDB slot entry: {}", &e);
                    NetError::InvalidStackerDBContract(contract_id.clone(), e)
                })?;

            if num_slots > STACKERDB_INV_MAX {
                let reason = format!(
                    "Contract {} stipulated more than maximum number of slots for one signer ({})",
                    contract_id, STACKERDB_INV_MAX
                );
                warn!("{}", &reason);
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }

            total_num_slots =
                total_num_slots
                    .checked_add(num_slots)
                    .ok_or(NetError::OverflowError(format!(
                        "Contract {} stipulates more than u32::MAX slots",
                        &contract_id
                    )))?;

            if total_num_slots > STACKERDB_INV_MAX {
                let reason = format!(
                    "Contract {contract_id} stipulated more than the maximum number of slots"
                );
                warn!("{reason}");
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }

            ret.push((addr, num_slots));
        }
        Ok(ret)
```

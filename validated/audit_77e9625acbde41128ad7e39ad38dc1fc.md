This confirms the analog: the `.signers` boot contract assigns the same signer address to slot indices in reward-cycle-specific StackerDB replicas (e.g., `signers-0-0`, `signers-1-0`, ...), where the slot assignment is separately tracked per contract but the signature made over a chunk never binds to which contract/reward-cycle it belongs to.The signature commitment gap that mirrors the reported `SOURCE_IDENTIFIER` replay bug exists in `libstackerdb`'s `SlotMetadata::auth_digest()`.

### Title
StackerDB chunk signatures omit the smart-contract/StackerDB identifier, enabling cross-database chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash` [1](#0-0) . There is no chain/network/contract-specific value folded into what gets signed, exactly analogous to the reported bug where a hardcoded `SOURCE_IDENTIFIER` (instead of a dynamic `chainId()`) let identical signed payloads validate on two different chains. Here, the sqlite schema comment for the `chunks` table explicitly documents that the signer address, data, and write time are "NOT covered by the signature" [2](#0-1)  — and notably, neither is the `stackerdb_id`/`smart_contract_id` that scopes a slot to a particular StackerDB replica.

### Finding Description
`try_replace_chunk` accepts a chunk if `slot_desc.verify(&slot_validation.signer)` passes, where `slot_validation.signer` is looked up per `(smart_contract, slot_id)` [3](#0-2) . The same check is duplicated in the network path via `validate_received_chunk`, which resolves the expected signer address with `get_slot_signer(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)` [4](#0-3) . Crucially, `verify()`/`auth_digest()` never receives or hashes `smart_contract_id` [5](#0-4) .

In production, the same signer StacksAddress is legitimately assigned to matching slot indices across multiple, independently-versioned StackerDB contracts — e.g. per-reward-cycle contracts `signers-0-*` and `signers-1-*`, whose slot assignments are populated from the same `.signers` boot contract state via `stackerdb-set-signer-slots` [6](#0-5) , and the `signers-{cycle_mod}-{message_id}` contract family shares slot-signer mappings [7](#0-6) . Because `auth_digest()` is identical for `(slot_id, slot_version, data_hash)` regardless of which contract/StackerDB it's destined for, a chunk signed and broadcast for StackerDB A can be relayed and re-validated as authentic in StackerDB B whenever: (1) the same signer occupies the same `slot_id` in both, and (2) B's `slot_validation.version` for that slot is less than the replayed `slot_version` (trivially true for a freshly (re)configured StackerDB where `NO_VERSION`/0 is the reset state, per `reconfigure_stackerdb`) [8](#0-7) .

This breaks the equality "signature authenticates *this* slot in *this* StackerDB replica" vs. what is actually checked ("signature authenticates slot_id+version+hash, independent of which replica/contract it is submitted to").

### Impact Explanation
This allows an unauthorized write of stale/foreign data into a StackerDB replica that was never actually produced by its rightful controller for that specific contract/reward-cycle — data that other nodes and the signer set treat as canonical replicated state. This aligns with the in-scope "StackerDB chunk stored without a valid owner signature" / "non-canonical state served as canonical" impact category, since the stored content did not originate as an authentic write for that specific DB instance, only for a different one, yet is accepted and replicated network-wide via `handle_unsolicited_StackerDBPushChunk` and sync download logic.

### Likelihood Explanation
Requires: (a) an attacker capturing/observing a validly-signed chunk from any StackerDB contract, and (b) a target StackerDB contract/reward-cycle where the same signer occupies the identical `slot_id` with a version lower than the captured chunk's version — a state that arises naturally whenever a new reward cycle's StackerDB is (re)provisioned (slots reset to `NO_VERSION`) while the same signer set continues to hold the same relative slot indices, as shown by `stackerdb-set-signer-slots`/`stackerdb-get-signer-slots-page` reusing signer-to-slot ordering across cycles.

### Recommendation
Fold a domain separator into `SlotMetadata::auth_digest()` — e.g., the `smart_contract_id` (or a `stackerdb_id`/network identifier) — so that signatures are cryptographically bound to the specific StackerDB replica they authenticate, mirroring how `chainId()` prevents cross-chain replay in the referenced Wormhole implementation.

### Proof of Concept
1. Signer `S` is registered for `slot_id = 0` in StackerDB contract `signers-0-block-response` for reward cycle N, and signs a `StackerDBChunkData{slot_id:0, slot_version:5, data:D}` which is broadcast and stored.
2. At reward cycle N+2 (`cycle_mod` repeats), `S` is again assigned `slot_id = 0` in the freshly reconfigured `signers-0-block-response` contract instance, whose slot version resets via `reconfigure_stackerdb` to `NO_VERSION` [9](#0-8) .
3. An observer replays the captured chunk (unchanged bytes, unchanged signature) against the new contract instance. `validate_received_chunk`/`try_replace_chunk` looks up the (new) `slot_validation.signer == S`, calls `slot_metadata.verify(&S)`, which succeeds because `auth_digest()` only depends on `(slot_id, slot_version, data_hash)` — none of which differ between the two contract instances — and the version-freshness check passes since 5 > 0. [10](#0-9) 
4. The stale/foreign chunk is accepted and stored as if it were an authentic write for the new StackerDB.

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

**File:** stackslib/src/net/stackerdb/db.rs (L302-346)
```rust
    pub fn reconfigure_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let mut total_slots_read = 0u32;
        for (principal, slot_count) in slots.iter() {
            total_slots_read =
                total_slots_read
                    .checked_add(*slot_count)
                    .ok_or(net_error::OverflowError(
                        "Slot count exceeeds u32::MAX".to_string(),
                    ))?;
            let slots_before_principal = total_slots_read - slot_count;
            for cur_principal_slot in 0..*slot_count {
                let slot_id = slots_before_principal + cur_principal_slot;
                if let Some(existing_validation) =
                    self.get_slot_validation(smart_contract, slot_id)?
                {
                    // this slot already exists.
                    if existing_validation.signer == *principal {
                        // no change
                        continue;
                    }
                }

                debug!("Reset slot {} of {}", slot_id, smart_contract);

                // new slot, or existing slot with a different signer
                let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
                let mut stmt = self.sql_tx.prepare(qry)?;
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];

                stmt.execute(args)?;
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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-717)
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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L15-24)
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

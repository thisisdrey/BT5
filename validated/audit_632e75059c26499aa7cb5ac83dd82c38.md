### Title
Cross-StackerDB chunk-signature replay due to missing contract-ID binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The authenticated digest used to validate every StackerDB chunk write (`SlotMetadata::auth_digest`) only commits to `slot_id`, `slot_version`, and `data_hash` — it never binds the signature to the `smart_contract_id` (i.e., which StackerDB the chunk is intended for). Because the Nakamoto `.signers-{parity}-{message_id}` StackerDB contracts all assign the *same* signer address to the *same* `slot_id` within a given signer-set parity, a validly-signed chunk produced for one message-type StackerDB (e.g. block responses) can be replayed and accepted into a different message-type StackerDB (e.g. transaction/proposal channel) that the attacker does not control, since nothing in the signature or verification path ties the authorization to the specific contract.

### Finding Description
`SlotMetadata::auth_digest` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`SlotMetadata::verify` recovers the public key from this digest and checks the resulting `Hash160` against the address passed in by the caller — again with no notion of which StackerDB/contract the signature belongs to: [2](#0-1) 

The two storage/validation call sites that ultimately accept and persist a chunk both look up the expected signer *per contract* and then call `verify()` on the (contract-agnostic) digest:
- Local push/pull validation: [3](#0-2) 
- Direct DB write path: [4](#0-3) 

The `smart_contract_id`/`stackerdb_id` is only used to select *whose* address is checked (via `get_slot_signer`/`get_slot_validation`), not to bind the signature itself. As long as the target contract also has the same address assigned to the same `slot_id` and an `expected_version`/stored version that is not higher than the replayed `slot_version`, the *same* signed bytes will verify successfully in the second contract.

This scenario is realistic in the Nakamoto signer StackerDB architecture: for a given reward-cycle parity, the same signer set/slot assignment is shared across all `.signers-{parity}-{message_id}` contracts, because each of those contracts simply proxies to the shared `.signers` contract's `stackerdb-get-signer-slots-page`: [5](#0-4) [6](#0-5) 

and confirmed by the test that iterates every `message_id` and shows the same expected slot list for a given parity/`signer_set`: [7](#0-6) 

Each `.signers-{parity}-{message_id}` StackerDB is instantiated as a separate, independent StackerDB with its own `stackerdb_id`/slot-version bookkeeping: [8](#0-7) 

So a signer's private key produces a signature over `(slot_id, slot_version, data_hash)` that authenticates writes to *any* StackerDB where that signer happens to occupy the same slot — including a different `message_id` contract of the same parity, or (should slot assignment/version state ever coincide) a different contract entirely. Nothing in `auth_digest`/`verify` distinguishes “this chunk is authorized for contract A’s slot 3” from “this chunk is authorized for contract B’s slot 3.”

This is the direct structural analog to the reported Across Protocol bug: the `slowRelayRoot` signature/commitment did not include the destination chain ID, so a valid relay authorized for one spoke chain could be replayed on any other spoke chain holding the token. Here, the chunk signature does not include the destination StackerDB (contract) identity, so a valid chunk write authorized for one StackerDB can be replayed into another StackerDB where the signer happens to hold the same slot.

### Impact Explanation
An attacker who observes (sniffs, or receives via normal gossip/relay) a validly signed `StackerDBChunkData` intended for one signer message-channel (e.g. a `BlockResponse` chunk) can resubmit the identical bytes to a *different* message-channel StackerDB contract for which the same signer address occupies the same slot and whose stored version does not exceed the chunk's version. This is a form of **network-wide propagation of forged/misattributed data**: content that was cryptographically authorized only for context A is accepted as authentic content for context B, without the actual slot owner ever intending or authorizing that write for B. Downstream consumers (signer message readers, coordinators, dashboards, other nodes replicating that specific StackerDB) may then process/relay this misattributed payload as if it were a legitimate signer message for that channel, potentially causing message-type confusion, spurious/duplicate state, or DoS-like disruption of the affected channel (e.g., unexpectedly using up a signer's slot version, blocking their real message on that channel with attacker-replayed content) — consistent with the "High" impact bracket in the ruleset (serving non-canonical/mismatched data as canonical) for the StackerDB in-scope code path.

### Likelihood Explanation
The attack requires no privileged access and no secret key: it only requires observing one validly signed chunk (chunks/gossip on StackerDB channels are, by design, publicly readable/relayed data, not secret) and resubmitting it verbatim to a different, unprivileged, remote HTTP/gossip endpoint (`POST /stackerdb/.../chunks` or via P2P `StackerDBPushChunk`). The precondition — the same signer address occupying the same slot ID across multiple `.signers-{parity}-{message_id}` contracts — is a structural property of the current signer-set design (confirmed via `signers.clar`/`signers-1-xxx.clar`), not a rare coincidence, making this readily reachable during normal Nakamoto signer operation. The remaining constraint (target slot version must not already exceed the replayed version) simply requires timing the replay against a channel/version that hasn't advanced past it yet, which is plausible in practice.

### Recommendation
Bind the signed digest to the specific StackerDB identity. Modify `SlotMetadata::auth_digest` (and correspondingly `sign`/`verify`) to also hash the `smart_contract_id` (or an equivalent unique identifier for the message-type/contract), so a signature cannot be replayed across different StackerDB contracts even when slot assignment is shared. This requires a corresponding wire/protocol change to `StackerDBChunkData` or its callers, since `contract_id` is not currently part of the chunk struct passed to `sign`/`verify`. All producers (`stacks-signer`) and consumers (`stackslib` chunk storage/validation) must be updated in lock-step, so a version-gated protocol change is warranted.

### Proof of Concept
1. Configure a Nakamoto network with an active reward cycle/parity where signer address `S` occupies `slot_id = k` in both `.signers-{parity}-0` (e.g. block-response channel) and `.signers-{parity}-1` (a different message channel), which is guaranteed by `signers.clar`'s shared `stackerdb-get-signer-slots-page`.
2. Signer `S` legitimately signs and pushes a `StackerDBChunkData { slot_id: k, slot_version: v, data: D }` to `.signers-{parity}-0` (using `StackerDBChunkData::sign`, whose digest is `auth_digest = H(slot_id || slot_version || data_hash)`), per: [9](#0-8) 
3. An attacker (any unprivileged network participant) captures this chunk (chunks are broadcast/replicated, per `net/stackerdb/mod.rs`/`sync.rs`) and resubmits the exact same `(slot_id, slot_version, sig, data)` tuple to `.signers-{parity}-1` via `POST /v2/stackerdb/{contract}/chunks` or `StackerDBPushChunk`, as long as `.signers-{parity}-1`'s stored version for slot `k` is `< v`.
4. `validate_received_chunk`/`try_replace_chunk` for `.signers-{parity}-1` looks up its own signer for slot `k` (which is also `S`), calls `slot_metadata.verify(&addr)`, and — because the digest never referenced the contract — the check passes: [3](#0-2) 
5. The chunk `D` (originally authored for the block-response channel) is now stored and propagated as legitimate content of the unrelated message channel `.signers-{parity}-1`, without signer `S` having authorized that write.

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

**File:** stackslib/src/net/stackerdb/db.rs (L225-260)
```rust
    /// Set up a database's storage slots.
    /// The slots must be in a deterministic order, since they are used to determine the chunk ID
    /// (and thus the key used to authenticate them)
    pub fn create_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        if slots.len() > (STACKERDB_INV_MAX as usize) {
            return Err(net_error::ArrayTooLong);
        }

        if self.get_stackerdb_id(smart_contract).is_ok() {
            return Err(net_error::StackerDBExists(smart_contract.clone()));
        }

        let qry = "INSERT OR REPLACE INTO databases (smart_contract_id) VALUES (?1)";
        let mut stmt = self.sql_tx.prepare(qry)?;
        let args = params![smart_contract.to_string()];
        stmt.execute(args)?;

        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;

        let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
        let mut stmt = self.sql_tx.prepare(qry)?;
        let mut slot_id = 0u32;

        for (principal, slot_count) in slots.iter() {
            test_debug!("Create StackerDB slots: ({}, {})", &principal, slot_count);
            for _ in 0..*slot_count {
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
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

**File:** stackslib/src/chainstate/stacks/boot/signers-1-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 1.
;; The contract name indicates which -- it has the form `signers-1-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u1))

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

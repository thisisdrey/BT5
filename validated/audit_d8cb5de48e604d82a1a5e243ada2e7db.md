## Title
StackerDB Chunk Signature Lacks Domain Separation (No Contract/Slot-Context Binding) Enabling Cross-Contract Chunk Replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The signature preimage used to authenticate a StackerDB chunk write commits only to `(slot_id, slot_version, data_hash)` and omits the target contract identity (`smart_contract_id`) or any network/session domain tag. Because the same signer address is assigned the *same* `slot_id` across multiple distinct StackerDB contracts (e.g. the `signers-0-{message_id}` / `signers-1-{message_id}` family used for block proposals, block responses, transactions, and DKG results), a validly-signed chunk produced for one contract is a syntactically valid, verifiable chunk for any other contract in which that address owns the same slot, as long as the version/max-writes checks pass there too.

### Finding Description
`SlotMetadata::auth_digest()` builds the signed digest solely from the slot id, slot version, and data hash: [1](#0-0) 

`verify()` recovers the signer purely from this digest and compares against the expected address for the given `(contract_id, slot_id)` pair looked up by the caller — the contract identity itself is never part of what was signed: [2](#0-1) 

The write-path validation (`validate_received_chunk`) fetches the expected signer address for the *specific* `smart_contract_id` being written to, then calls `slot_metadata.verify(&addr)`, which never re-checks that the signature was originally produced *for that contract*: [3](#0-2) 

Slot assignment is derived from `.signers` contract state and is identical across every message-type-specific StackerDB for a given signer set (`signers-0-0`, `signers-0-1`, `signers-0-2`, `signers-0-3`, …), since all of them read the same slot list via `stackerdb-get-signer-slots-page`: [4](#0-3) [5](#0-4) 

and the node provisions one instance of this boot-code contract pair per `message_id` per signer set, so a given signer address occupies the same `slot_id` in every one of those contracts simultaneously: [6](#0-5) 

Because the signed digest never binds to a `smart_contract_id` (nor to a chain/network identifier), a chunk `(slot_id, slot_version, data, sig)` that a signer legitimately wrote to, say, the `BlockResponse` StackerDB is *also* a validly-verifying chunk for the `Transactions` or `DkgResults` StackerDB (or any other contract where the address owns the same slot), provided the target contract's expected version for that slot is `<= slot_version` and `max_writes` is not exceeded. Any unprivileged peer that observes a gossiped/pushed chunk can relay it to a different StackerDB endpoint (`handle_unsolicited_StackerDBPushChunk`, or by pushing/serving it during sync via `validate_received_chunk`) and have it accepted as "signed data from that principal" in a context the signer never authorized.

This is the direct analog of the reported Solidity bug: the signed payload lacks domain separation (there, `chainid`/`address(this)`/`currentRoundId`; here, `smart_contract_id`/network context), so a signature valid in one context is replayable in another where the same signer/slot mapping happens to coincide.

### Impact Explanation
This allows an unprivileged remote peer to inject forged (replayed) data chunk content into a StackerDB contract that the signer never intended to write to, as long as slot ownership coincidentally overlaps and version/`max_writes` constraints are satisfiable — i.e., network-wide propagation of misattributed/forged data under a legitimate signer's identity into the wrong logical channel (e.g., stale `BlockResponse` bytes propagated as if freshly written to the `Transactions` or `DkgResults` channel). Downstream consumers (`stacks-signer`, block proposal listeners) that trust `slot_metadata.verify(addr)` as proof that "signer X authored this data for this StackerDB" would process attacker-relayed, mis-contextualized content as authentic. This does not grant arbitrary content injection (the attacker cannot forge new data, only replay previously-signed bytes into a different contract/slot context), but it breaks the assumed 1:1 binding between a StackerDB signature and its target replica, enabling propagation of forged/misattributed data across contract boundaries.

### Likelihood Explanation
Likelihood is **moderate**: the attacker needs no privileges — any relay-capable network peer can capture a valid chunk from the peer-to-peer gossip/push path and resend it against a different but overlapping StackerDB contract. The main constraint is that the target contract's currently-expected slot version for that slot must be `<=` the replayed chunk's version, and `max_writes` not yet exceeded — both of which are easily satisfiable for freshly-created reward-cycle StackerDB instances (which restart versioning) or for slots that have not yet been written in the target contract.

### Recommendation
Bind the signed digest to the specific StackerDB context, not just slot metadata. `SlotMetadata::auth_digest()` should include the `smart_contract_id` (and network id or chain id) in the hashed preimage, e.g.:

```rust
fn auth_digest(&self, contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(contract_id.serialize_to_vec()); // domain separation
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```

`sign`/`verify` and all call sites (`StackerDBChunkData::sign`, `verify`, `recover_pk`, `validate_received_chunk`) must be updated to thread the target `smart_contract_id` through, so a signature computed for one StackerDB contract can never verify successfully for another.

### Proof of Concept
1. Let address `A` be assigned `slot_id = 3` in both `signers-0-1` (BlockResponse) and `signers-0-3` (DkgResults) contracts for the current reward cycle (a routine outcome of `stackerdb-get-signer-slots-page` returning the identical slot list to every `signers-0-*` contract).
2. Signer `A` legitimately signs and pushes `StackerDBChunkData { slot_id: 3, slot_version: 5, data: <BlockResponse bytes>, sig }` to `signers-0-1`.
3. An observing (unprivileged) peer captures this chunk from gossip/push traffic.
4. That peer resends the identical `StackerDBChunkData` (same `slot_id`, `slot_version`, `data`, `sig`) as a `StackerDBPushChunk`/`PutChunk` targeting `signers-0-3` (DkgResults), where `A` also owns slot 3, and the target's current expected version for slot 3 is `<= 5` and writes remain under `max_writes`.
5. `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs` looks up `get_slot_signer(signers-0-3, 3)` → `A`, then calls `slot_metadata.verify(&A)`, which succeeds because `auth_digest()` never included the contract identity — the chunk is accepted as authentic `DkgResults` data from `A`, even though `A` never signed anything for that contract. [7](#0-6) [8](#0-7)

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-697)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

**File:** stackslib/src/clarity_vm/clarity.rs (L1785-1806)
```rust
            // stackerdb contracts for each message type
            for signer_set in 0..2 {
                for message_id in 0..SIGNER_SLOTS_PER_USER {
                    let signers_name =
                        NakamotoSigners::make_signers_db_name(signer_set, message_id);
                    let body = if signer_set == 0 {
                        SIGNERS_DB_0_BODY
                    } else {
                        SIGNERS_DB_1_BODY
                    };
                    let payload = TransactionPayload::SmartContract(
                        TransactionSmartContract {
                            name: ContractName::try_from(signers_name.clone())
                                .expect("FATAL: invalid boot-code contract name"),
                            code_body: StacksString::from_str(body)
                                .expect("FATAL: invalid boot code body"),
                        },
                        Some(ClarityVersion::Clarity2),
                    );

                    let signers_contract_tx =
                        StacksTransaction::new(tx_version, boot_code_auth.clone(), payload);
```

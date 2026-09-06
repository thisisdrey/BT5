### Title
Missing contract-id binding in `SlotMetadata` signature digest allows StackerDB chunk replay across sibling `.signers-*` contracts - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest for a StackerDB chunk from only `slot_id`, `slot_version`, and `data_hash`. It never binds the digest to the StackerDB's `QualifiedContractIdentifier`. Because the Nakamoto signer subsystem creates many sibling StackerDB contracts per reward cycle (`signers-{0,1}-{message_id}`) that all share the *same* signer-to-slot-id assignment, a chunk that is validly signed for one contract (e.g. a `BlockResponse` message in `signers-0-0`) is also a valid signature for the *same slot* in a different message-type contract (e.g. `signers-0-1`), since the verifying code never checks contract identity as part of the cryptographic commitment.

### Finding Description
`SlotMetadata::auth_digest()` is: [1](#0-0) 

and `verify()`/`sign()` operate solely on this digest: [2](#0-1) 

Note that the contract/StackerDB context is never part of the hashed material — only `slot_id`, `slot_version`, and `data_hash`.

On the verification side, `StackerDBSync::validate_received_chunk` (used both for downloaded chunks and unsolicited pushed chunks) looks up the expected signer address using the target `smart_contract_id`, but then verifies the signature using only that address against the contract-agnostic digest: [3](#0-2) 

The signer subsystem creates one StackerDB contract per `(reward_cycle_parity, message_id)` pair: [4](#0-3) 

and each of these sibling contracts (`signers-0-0`, `signers-0-1`, `signers-0-2`, ...) derives its slot-to-signer assignment from the *same* underlying list, via `stackerdb-get-signer-slots-page`: [5](#0-4) [6](#0-5) 

Because the slot-to-signer-address map is identical across all `signers-{set}-{message_id}` contracts for a given reward cycle, `get_slot_signer(contract_id, slot_id)` returns the same `StacksAddress` for slot N regardless of which sibling contract is queried. Since the signature digest never incorporates `contract_id`, a chunk `(slot_id, slot_version, data_hash, sig)` legitimately produced and signed by signer X for contract `signers-0-0` will also pass `SlotMetadata::verify()` when submitted against slot N of `signers-0-1` (or any other sibling message-id contract), as long as the target slot's stored version is lower than the replayed `slot_version`.

This is directly analogous to the reported KZG issue: the Fiat-Shamir-style commitment (`auth_digest`) omits a piece of context that the verifier actually relies on (the contract/domain identifier), allowing an attacker to satisfy the "authenticated" check in a context the signer never intended.

### Impact Explanation
An unprivileged network peer that observes a legitimately signed chunk in one signer StackerDB contract can resubmit (relay) that exact same `StackerDBChunkData` to a sibling `signers-{set}-{message_id}` contract at the same slot ID. Since `try_replace_chunk`/`validate_received_chunk` only checks the recovered address against the (contract-agnostic) digest, the replayed chunk is accepted as validly signed, stored to the node's replica, and gossiped network-wide to other replicating peers as authentic data for the wrong contract — an unauthorized write to StackerDB state and propagation of data that was never actually signed for that destination contract. This matches the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact bucket, since acceptance and gossip happen purely from signature-verification logic in `stackslib/src/net/stackerdb/mod.rs`, without any node operator or private-key involvement by the attacker.

### Likelihood Explanation
The signer StackerDB architecture (`signers-0-{message_id}` / `signers-1-{message_id}`) is deployed in every Nakamoto-epoch network by design, so the precondition (multiple sibling contracts sharing one slot/signer map) always holds in production, not just in a contrived test setup. Any peer can observe signer chunks (they are broadcast/relayed StackerDB data) and simply retransmit the identical bytes to a different contract endpoint — no cryptography needs to be broken, only a message replayed to a different destination. The remaining gating factor is that the target slot's chunk version must be lower than the replayed one, which is trivially true for freshly-configured or low-traffic sibling contract slots.

### Recommendation
Bind the `QualifiedContractIdentifier` (or an equivalent, unambiguous StackerDB domain identifier) into `SlotMetadata::auth_digest()` before hashing, so the signature commits to exactly which StackerDB/contract the chunk is destined for, not merely `slot_id`/`slot_version`/`data_hash`. This requires updating `sign`/`verify`/`auth_digest` in `libstackerdb/src/libstackerdb.rs`, and passing the contract id through the existing call sites in `stackslib/src/net/stackerdb/mod.rs` and `stackslib/src/net/stackerdb/db.rs`, mirroring the external report's general guidance to include *all* verifier-relied-upon, prover-controllable inputs in the committed digest.

### Proof of Concept
1. Let signer X own slot 5 in both `signers-0-0` and `signers-0-1` for the current reward cycle (guaranteed by `stackerdb-get-signer-slots-page`, which returns identical lists regardless of the `page`/message_id argument used by both contracts within the same set).
2. Signer X publishes a legitimately-signed `StackerDBChunkData { slot_id: 5, slot_version: 1, sig, data }` to `signers-0-0` (e.g., a `BlockResponse` message). The signature verifies via `SlotMetadata::verify()` in `libstackerdb/src/libstackerdb.rs:183-193`, computed only from `(5, 1, data_hash)`.
3. An attacker captures this chunk (visible via StackerDB gossip/`StackerDBGetChunkData`) and submits the identical bytes to the `signers-0-1` contract (currently at slot 5 version 0).
4. `StackerDBSync::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-717`) resolves the expected signer for `(signers-0-1, 5)` — the same address X — and calls `slot_metadata.verify(&addr)`, which succeeds because the digest never encoded which contract it belonged to.
5. The chunk is accepted, stored, and gossiped as authentic slot data for `signers-0-1`, even though signer X never signed anything intended for that contract.

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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1073)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-6)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

```

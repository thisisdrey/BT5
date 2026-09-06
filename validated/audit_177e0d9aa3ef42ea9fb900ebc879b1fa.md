### Title
Cross-contract StackerDB chunk signature replay via `SlotMetadata::auth_digest` omitting contract identifier - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` computes the signed digest from only `slot_id`, `slot_version`, and `data_hash`, never the `QualifiedContractIdentifier` of the StackerDB the chunk belongs to. As a result, a valid `StackerDBChunkData` signature produced for one StackerDB contract is equally valid for any other contract in which the signer's address is registered for the same `slot_id`, since `StackerDBSync::validate_received_chunk` verifies the signature against the per-contract registered address without binding the signature to the contract itself.

### Finding Description
`SlotMetadata::auth_digest` hashes `slot_id`, `slot_version`, and `data_hash` only [1](#0-0) , and `verify`/`sign` operate exclusively on this digest [2](#0-1) . The contract identity is never mixed into the signed material.

Validation of an incoming chunk (`StackerDBSync::validate_received_chunk`) looks up the expected signer via `self.stackerdbs.get_slot_signer(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)` [3](#0-2) . Because the digest doesn't include `smart_contract_id`, this check only constrains that *some* address registered for `slot_id` in contract Y matches the recovered pubkey hash from the signature — it does not verify that the signer intended the chunk for contract Y specifically.

In deployments where the same signer address is registered for the same `slot_id` across multiple StackerDB contracts (e.g. Stacks Nakamoto signer StackerDB contracts across reward cycles/message lanes, where slot assignment order mirrors the signer set), a signature produced and broadcast for contract X's slot N is cryptographically indistinguishable from one intended for contract Y's slot N. This is confirmed by the existing test `test_handle_unsolicited_stackerdb_push_chunk_future_view_validation`, which shows that acceptance hinges only on `slot_id`/`slot_version`/`data`/signature matching the registered signer for whatever `contract_id` is presented in the `StackerDBPushChunkData` message, with no distinguishing bytes from the contract itself [4](#0-3) .

### Impact Explanation
This is a signature-binding defect (missing domain separation) rather than a demonstrated unauthenticated-write vulnerability in this codebase: `get_slot_signer` is looked up per contract, so an attacker who is *not* a registered signer for the target contract's `slot_id` still cannot forge a valid signature there — verification would fail against the different registered address. The exploitable case requires that the same address already legitimately owns `slot_id N` in both contract X and contract Y, in which case that address could already write arbitrary signed data directly into contract Y without needing to replay anything. The interesting residual risk is a *replay* by a third party: intercepting an honest signer's genuinely-broadcast chunk for contract X and re-injecting it into contract Y's slot N (same address, same slot_id), causing contract Y to store data the signer authorized only for X. Whether this is reachable in practice depends on StackerDB contract/slot assignment configuration (same address at the same slot index across contracts), which is a deployment/protocol-level precondition not established as always true from this repo's code alone.

### Likelihood Explanation
Requires: (1) two StackerDB contracts where the same signer address occupies the identical `slot_id`, and (2) the attacker capturing (or already knowing) a validly-signed chunk broadcast by that signer for contract X. The attacker does not need the private key and can relay via `StackerDBPushChunkData` over P2P using only bytes they observed, which is within the "unprivileged remote party" threat model. However, without confirming that current mainnet/testnet StackerDB contract configurations actually assign identical slot indices to the same address across contracts, this remains a latent cryptographic weakness rather than a demonstrated concrete cross-contract forgery in the current test/config surface examined.

### Recommendation
Include the `QualifiedContractIdentifier` (or a chain/network-scoped domain separator) as part of `SlotMetadata::auth_digest`, e.g., hash the contract's issuer address and name bytes alongside `slot_id`, `slot_version`, and `data_hash`, so a signature is cryptographically bound to a specific StackerDB contract and cannot be replayed into another.

### Proof of Concept
Rust test plan (extending `stackslib/src/net/stackerdb/tests/db.rs` or `stackslib/src/net/tests/relay/nakamoto.rs`):
1. Create two StackerDB contracts, `contract_x` and `contract_y`, each registering the same `signer_addr` (from `signer_privk`) at `slot_id = 0`.
2. Build a `StackerDBChunkData::new(0, 1, data)` and sign it with `signer_privk` — this only commits to `(slot_id=0, slot_version=1, data_hash)`, per `SlotMetadata::auth_digest` at `libstackerdb/src/libstackerdb.rs:160-166`.
3. Call `stackerdbs.get_slot_signer(&contract_x, 0)` and `stackerdbs.get_slot_signer(&contract_y, 0)`, confirm both equal `signer_addr`.
4. Call `chunk_data.get_slot_metadata().verify(&signer_addr)` — assert `Ok(true)` for both contract-X and contract-Y usage contexts, proving the identical signature validates in both, i.e., the "expected" verification path (`validate_received_chunk`) accepts the chunk under `contract_y` even though it was never signed with any reference to `contract_y`.
5. Assertion point: `SlotMetadata::verify` at `libstackerdb/src/libstackerdb.rs:183-193` returns `true` in both cases with no differentiation by contract, demonstrating the missing domain separation.

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

**File:** stackslib/src/net/tests/relay/nakamoto.rs (L1225-1247)
```rust
    let mut good_chunk_data = StackerDBPushChunkData {
        contract_id: contract_id.clone(),
        rc_consensus_hash: future_consensus_hash.clone(),
        chunk_data: StackerDBChunkData::new(0, 1, vec![1, 2, 3, 4, 5]),
    };
    good_chunk_data.chunk_data.sign(&signer_privk).unwrap();

    let result = peer
        .network
        .handle_unsolicited_StackerDBPushChunk(
            &mut stacks_node.chainstate,
            1,
            &preamble,
            &good_chunk_data,
            false,
        )
        .unwrap();

    assert_eq!(
        result,
        (true, false),
        "chunk with valid signature must be buffered on FutureView path"
    );
```

### Title
Missing StackerDB contract-id (domain separation) in `SlotMetadata::auth_digest` allows chunk-signature replay across StackerDB contracts - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the payload that is actually signed and verified for every StackerDB chunk — commits only to `slot_id`, `slot_version`, and `data_hash`. It does not commit to the StackerDB smart-contract identifier (`QualifiedContractIdentifier`) that the chunk is destined for, nor to any chain/network id. Because a single signer key is frequently a registered signer for the same `slot_id` across multiple distinct StackerDB replicas (e.g. the `.signers-<set>-<message_id>` boot contracts, which are populated with the same reward-set ordering per message lane), a validly-signed chunk observed on one StackerDB contract can be replayed verbatim onto a different StackerDB contract and will pass signature verification there as well.

### Finding Description
The signing/verification logic lives in `libstackerdb/src/libstackerdb.rs`: [1](#0-0) 

`auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`. `verify()` recovers the public key from this digest and the signature and compares it to the expected `StacksAddress` for that slot: [2](#0-1) 

The consumer of this check, `StackerDBs::validate_received_chunk`, resolves the *expected* signer via `get_slot_signer(smart_contract_id, data.slot_id)` but then calls `slot_metadata.verify(&addr)`, which never mixes `smart_contract_id` into the signed digest: [3](#0-2) 

This is the same class of bug the external report describes for `matchOrder`/`buyPosition`: a signature scheme lacking a domain separator (contract/chain identity) means a signature produced in one context is indistinguishable from — and accepted in — a different context, i.e. "signature reuse across different projects/chains" translates here to "signature reuse across different StackerDB contracts."

Both the pull-based sync path (`validate_downloaded_chunk` → `validate_received_chunk`) and the unsolicited push path (`handle_unsolicited_StackerDBPushChunk`) rely on the exact same `verify()`/`auth_digest()` primitive, so both remote ingestion paths are affected. The tests confirm the digest's scope is limited to `(slot_id, slot_version, data_hash)` and nothing else: [4](#0-3) 

Multiple StackerDB contracts commonly share overlapping signer sets and slot assignments — for instance the `.signers-<reward-cycle>-<message-id>` contracts (message-id 0/1 lanes) and the `.miners` contract are all populated by iterating the same reward set / miner list. Since slot IDs are assigned deterministically by iteration order over the signer/miner list when a StackerDB is created, the same key frequently maps to the same `slot_id` in more than one contract: [5](#0-4) 

Given that, a signature made by a signer over `(slot_id, slot_version, data_hash)` for contract A validates unchanged for contract B if that signer also owns `slot_id` there and the data/version happen to line up (or an adversary crafts a colliding version/data on the target contract and observes/replays a broadcast chunk).

### Impact Explanation
An unprivileged, remote attacker who observes a legitimately-signed StackerDB chunk gossiped across the p2p network (StackerDB chunks are broadcast/replicated data, not secret) can resubmit the exact same `(slot_id, slot_version, sig, data)` tuple to a *different* StackerDB contract endpoint (either via the `POST /stackerdb_chunks` RPC or via unsolicited `StackerDBPushChunk` p2p messages) that also happens to register the same address for that slot. Because the signature never binds to the contract identity, the node will accept and store the chunk as authentic for the "wrong" StackerDB, i.e., forged/foreign data gets propagated and stored as canonical content for a contract it was never intended for. This can pollute the miner/signer coordination data plane (e.g. `.miners` vs `.signers-X-Y` lanes), causing consumers such as `TryFrom<StackerDBChunksEvent> for SignerEvent` to process attacker-controlled, cross-context data as though it were legitimate for that lane — matching the "network-wide propagation of forged data" / "unauthorized write to StackerDB" high/critical impact categories in scope.

### Likelihood Explanation
Exploitation requires no privileged access: StackerDB chunk data is public/gossiped, and both ingestion paths (`POST /stackerdb_chunks` and unsolicited push) are unauthenticated aside from this signature check. The main precondition — that the same signer address own the same `slot_id` across two different StackerDB contracts — depends on how contract configurations assign slots, which I was not able to fully verify from the indexed portions of `stackslib/src/chainstate/nakamoto/signer_set.rs` (the exact per-message-lane slot-ordering logic). This uncertainty affects how broadly exploitable the collision is in the current mainnet contract configuration, but the root-cause primitive itself — a signature with no domain/contract separation — is unambiguously present and remotely reachable through both chunk-ingestion code paths shown above.

### Recommendation
Include the target `QualifiedContractIdentifier` (and ideally a chain/network id) inside `SlotMetadata::auth_digest()`, e.g. hash `smart_contract_id.to_string()` (or its serialized bytes) alongside `slot_id`, `slot_version`, and `data_hash`, and thread that context through `sign()`/`verify()`/`recover_pk()`. This requires updating the on-the-wire `StackerDBChunkData`/`SlotMetadata` sign/verify call sites in `stackslib/src/net/stackerdb/mod.rs` and `libstackerdb/src/libstackerdb.rs` to pass the contract id into the digest computation, plus a coordinated protocol/version bump since it changes the signed payload.

### Proof of Concept
1. Configure two StackerDB contracts, A and B, such that address `X` (private key `sk`) is the registered signer for `slot_id = 0` in both (achievable today whenever a signer participates in multiple `.signers-*-*` lanes or `.miners`, since slot assignment order can coincide).
2. Attacker observes on the p2p network (or queries) a valid chunk for contract A: `StackerDBChunkData { slot_id: 0, slot_version: v, sig, data }`, signed by `sk` — this signature only commits to `(0, v, hash(data))` per `auth_digest()` (`libstackerdb/src/libstackerdb.rs:159-166`).
3. Attacker resubmits the identical tuple to contract B via `POST /stackerdb_chunks` (or as an unsolicited `StackerDBPushChunk`) targeting B's contract id.
4. `validate_received_chunk` for contract B looks up B's expected signer for slot 0, sees it is `X`, and calls `slot_metadata.verify(&X)` — which succeeds because the digest never included the contract id (`stackslib/src/net/stackerdb/mod.rs:679-697`).
5. The chunk (originally intended for A) is accepted and stored as valid data for B, even though `X` never signed anything referencing B.

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

**File:** libstackerdb/src/tests/mod.rs (L26-73)
```rust
#[test]
fn test_stackerdb_slot_metadata_sign_verify() {
    let pk = StacksPrivateKey::random();
    let addr = StacksAddress::from_public_keys(
        C32_ADDRESS_VERSION_MAINNET_SINGLESIG,
        &AddressHashMode::SerializeP2PKH,
        1,
        &vec![StacksPublicKey::from_private(&pk)],
    )
    .unwrap();
    let bad_addr = StacksAddress::new(0x01, Hash160([0x01; 20])).unwrap();

    let chunk_data = StackerDBChunkData {
        slot_id: 0,
        slot_version: 1,
        sig: MessageSignature::empty(),
        data: vec![0x1; 128],
    };

    let mut slot_metadata = chunk_data.get_slot_metadata();
    slot_metadata.sign(&pk).unwrap();

    assert!(slot_metadata.verify(&addr).unwrap());

    // succeeds with high-S signature (that's not necessarily good, but
    // since this has always worked, it can't just stop)
    slot_metadata.signature = slot_metadata.signature.with_negated_s();
    assert!(slot_metadata.verify(&addr).unwrap());

    // fails with wrong address
    assert!(!slot_metadata.verify(&bad_addr).unwrap());

    // fails with corrupted data
    let mut bad_slot_metadata = chunk_data.get_slot_metadata();
    bad_slot_metadata.sign(&pk).unwrap();
    bad_slot_metadata.slot_id += 1;
    assert!(!bad_slot_metadata.verify(&addr).unwrap());

    let mut bad_slot_metadata = chunk_data.get_slot_metadata();
    bad_slot_metadata.sign(&pk).unwrap();
    bad_slot_metadata.slot_version += 1;
    assert!(!bad_slot_metadata.verify(&addr).unwrap());

    let mut bad_slot_metadata = chunk_data.get_slot_metadata();
    bad_slot_metadata.sign(&pk).unwrap();
    bad_slot_metadata.data_hash = Sha512Trunc256Sum([0x20; 32]);
    assert!(!bad_slot_metadata.verify(&addr).unwrap());
}
```

**File:** stackslib/src/net/stackerdb/db.rs (L225-269)
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
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];
                stmt.execute(args)?;

                slot_id += 1;
            }
        }
```

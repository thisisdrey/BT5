This confirms the schema: chunks/slots are scoped per `stackerdb_id` (i.e., per smart contract), but the cryptographic signature binding (`auth_digest` in `libstackerdb/src/libstackerdb.rs`) does not include any contract identifier at all.

### Title
Missing StackerDB contract identifier in chunk signature digest allows cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` (and thus the signature carried in `StackerDBChunkData::sig`) commits only to `slot_id`, `slot_version`, and `data_hash` — never to the target StackerDB smart contract identifier. Because slot ownership (which `StacksAddress` is authorized to write a given `slot_id`) is looked up per-contract (`smart_contract_id` in the `databases`/`chunks` schema), but the signed digest has no contract binding, a chunk validly signed by an authorized writer for one StackerDB contract can be replayed by any relaying peer into a *different* StackerDB contract where that same address happens to own the same `slot_id`, causing unauthorized data from one application context to be accepted and stored as legitimate content in another.

### Finding Description
`SlotMetadata::auth_digest` at [1](#0-0)  computes the signed digest strictly as `hash(slot_id || slot_version || data_hash)`. `SlotMetadata::verify` at [2](#0-1)  recovers the public key from this digest and checks only that the recovered address matches the expected slot owner passed in by the caller — the smart contract identifier is never part of what is signed.

However, authorization is inherently per-contract: slot ownership records are keyed by `smart_contract_id` in the sqlite schema (`databases` table joined to `chunks`) as seen in [3](#0-2) , and both `StackerDBTx::try_replace_chunk` [4](#0-3)  and `PeerNetwork::validate_received_chunk` [5](#0-4)  look up "who is the authorized signer of slot X **in contract C**" and then verify the chunk's signature against that address — without ever confirming the signature was produced *for contract C specifically*.

This breaks the equality that should hold: "signature authenticates (contract, slot_id, slot_version, data)" but in reality it only authenticates "(slot_id, slot_version, data)". Any two StackerDB contracts that happen to assign the same `slot_id` to the same signing address (a very common pattern in this codebase, e.g. successive `.signers-<cycle>-<n>` contracts across reward cycles typically preserve the same slot index for the same signer key) become cross-replayable: a chunk broadcast on the network for contract A's slot can be relayed and accepted into contract B's identical slot by any observing, unprivileged peer, with no knowledge of any private key required (the attacker only needs to capture and re-relay the publicly gossiped `StackerDBChunkData`/`StackerDBPushChunkData` message under a different `contract_id`).

### Impact Explanation
This allows a remote, unprivileged peer to cause a node to store/propagate data into a StackerDB slot as "signed by the authorized owner" when that data was never authorized/intended for that specific contract/application context. This is a stored/authenticated-data mismatch: content that the network will treat as canonical for contract B is actually only authenticated for contract A. Depending on which contract is targeted (e.g., signer message StackerDBs used for Nakamoto block signing coordination), this can result in stale or cross-context data being accepted as fresh, valid signer-application data, and propagated network-wide via `handle_unsolicited_StackerDBPushChunk` [6](#0-5) .

### Likelihood Explanation
Exploitation requires no secrets: an attacker only needs to observe one legitimately-signed, network-broadcast `StackerDBChunkData` from any StackerDB contract and identify a second StackerDB contract where the signing address is also a registered slot owner of the same `slot_id` (with a compatible/older expected version). This is a realistic condition in deployments that reuse the same signer key/slot index scheme across multiple StackerDB contracts (e.g., across reward cycles).

### Recommendation
Bind the target contract identifier (and ideally the network/chain identifier) into `SlotMetadata::auth_digest`, e.g. `hash(smart_contract_id || slot_id || slot_version || data_hash)`, and update `sign`/`verify` call sites in [4](#0-3)  and [5](#0-4)  to pass in and check the contract identifier as part of the digest.

### Proof of Concept
1. Signer `S` is registered as the owner of `slot_id = 3` in StackerDB contract `A` (e.g. `.signers-1-1`) and also happens to own `slot_id = 3` in contract `B` (e.g. `.signers-2-1`), a common configuration since slot indices are typically kept stable per signer across contracts.
2. `S` legitimately signs and pushes a chunk `(slot_id=3, slot_version=5, data=D)` for contract `A`; this is broadcast via `StackerDBPushChunk` and observable by any peer.
3. A malicious peer captures this `StackerDBChunkData` (unmodified: same `slot_id`, `slot_version`, `sig`, `data`) and relays it wrapped in a `StackerDBPushChunkData` whose `contract_id` field is set to `B` instead of `A`.
4. The receiving node's `validate_received_chunk` ( [7](#0-6) ) looks up the slot-3 owner for contract `B`, finds `S`, and `slot_metadata.verify(&addr)` succeeds because the digest never included the contract ID — the chunk is accepted as valid for `B` even though `S` never authorized this data for contract `B`'s slot 3, and if the version check passes it is committed via `try_replace_chunk` and further relayed network-wide.

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

**File:** stackslib/src/net/stackerdb/db.rs (L35-51)
```rust
const STACKER_DB_SCHEMA: &[&str] = &[
    r#"
    PRAGMA foreign_keys = ON;
    "#,
    r#"
    CREATE TABLE databases(
        -- internal numeric identifier for this stackerdb's smart contract identifier
        -- (so we don't have to copy it into each chunk row)
        stackerdb_id INTEGER NOT NULL,
        -- smart contract ID for this stackerdb
        smart_contract_id TEXT UNIQUE NOT NULL,
        PRIMARY KEY(stackerdb_id)
    );
    "#,
    r#"
    CREATE INDEX on_database_contract_names ON databases(smart_contract_id);
    "#,
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

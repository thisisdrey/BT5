### Title
StackerDB Chunk Signature Omits Contract/Namespace Binding, Enabling Cross-StackerDB Chunk Replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The signature that authenticates a StackerDB chunk write is computed over `slot_id`, `slot_version`, and `data_hash` only. It never binds the signature to the specific StackerDB (`smart_contract_id`/namespace) the write is destined for. Because the same signer address is frequently assigned the same relative `slot_id` across multiple distinct StackerDB contracts (e.g. the paired `.signers-0-xxx` / `.signers-1-xxx` message-topic contracts derived from the same underlying signer set), a validly-signed chunk for one StackerDB can be replayed as a validly-signed chunk into a different StackerDB, as long as the version/freshness checks are satisfied there too.

### Finding Description
`SlotMetadata::auth_digest()` computes the digest that is signed and verified for every StackerDB chunk write: [1](#0-0) 

```
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```

`sign()`/`verify()` operate purely on this digest, and `verify()` only checks that the recovered public key hash matches the address the caller supplies: [2](#0-1) 

The contract/namespace context is supplied out-of-band by the caller (`validate_received_chunk` looks up the expected signer via `get_slot_signer(smart_contract_id, slot_id)`), not by anything inside the signed digest: [3](#0-2) 

Because the digest is `(slot_id, slot_version, data_hash)` only, a signature that is valid for `slot_id=X, version=V, data_hash=H` in StackerDB `A` is equally valid for `slot_id=X, version=V, data_hash=H` in StackerDB `B`, provided the same signer address `S` happens to be assigned `slot_id=X` in `B` as well and `B`'s freshness/version rules are satisfied. This is a structural gap, not a wrong-signer bug: `verify()` correctly rejects wrong signers, but nothing prevents a *correct* signer's chunk from being replayed cross-context because the digest never encodes which StackerDB it is scoped to.

This condition is realistic in this codebase because multiple signer-message StackerDB contracts are derived from the same underlying reward-cycle signer set (e.g. paired odd/even reward-cycle signer contracts named via `make_signers_db_contract_id(reward_cycle, message_id, mainnet)`), so the same signer frequently occupies the same relative slot index across different StackerDB contracts/topics: [4](#0-3) 

The slot assignment for any StackerDB contract is purely a function of the contract's own `stackerdb-get-signer-slots` call, ordered the same way the underlying signer set is ordered: [5](#0-4) 

so any two StackerDB contracts sharing the same signer-set ordering will assign identical `(address, slot_id)` pairings, satisfying the replay precondition without any special effort by the attacker.

### Impact Explanation
An entity that is a legitimate signer for slot `X` in StackerDB `A` and also holds slot `X` in a different StackerDB `B` (a common configuration, since separate message-topic StackerDBs share the same signer-set ordering) can take a chunk it validly signed for `A` — or one intercepted from network relay/gossip for `A` — and push it into `B`'s slot `X`, where it will pass `validate_received_chunk`'s signer and version checks and be accepted as authentic data for `B`. This lets forged/foreign data be propagated network-wide into a StackerDB namespace it was never intended for, being served/read by peers and application logic as canonical, topic-specific data. Any consumer that parses `B`'s chunk contents assuming a particular message schema (e.g. Nakamoto signer message types) can be fed data crafted for a completely different schema/topic, corrupting the derived read-side state, or causing message-type mismatches that peers/signers must handle unexpectedly. This satisfies "network-wide propagation of forged data" / "serving non-canonical state as canonical" without needing the target's private key — only the actor's own valid signature over a different context is reused.

### Likelihood Explanation
The precondition (holding the same slot index across two StackerDB contracts sharing signer-set ordering) is a natural, not adversarially-contrived, side effect of how paired/topic StackerDB contracts are provisioned in this codebase, so no privileged access or secret key is required beyond what the actor already legitimately possesses (its own signing key and being a registered signer in at least one relevant StackerDB). The write itself is remote and requires only a handful of P2P/RPC messages (`StackerDBPushChunk` or the `POST /stackerdb chunk` API), matching the "few messages, unauthenticated/unauthorized write to state" bar. The main uncertainty is how many deployed StackerDB pairs actually share identical slot orderings in practice (I could not fully confirm from the indexed subset whether `message_id` variance always preserves relative ordering across every contract instantiation, since the full Clarity contract logic for `signers-0-xxx.clar`/`signers-1-xxx.clar` slot-page assembly was only partially visible); this is noted as an open point that a Devin session with full file access could verify precisely.

### Recommendation
Bind the signature to the StackerDB's namespace by including the `smart_contract_id` (or a stable hash of it) inside `SlotMetadata::auth_digest()`, e.g.:
```
hasher.update(contract_id.serialize_to_vec());
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
```
This requires threading the `QualifiedContractIdentifier` through `sign()`/`verify()`/`get_slot_metadata()` and updating both the write path (`try_replace_chunk`) and the validation path (`validate_received_chunk`) to pass in the contract ID being written to, ensuring a signature can never be valid for more than one StackerDB.

### Proof of Concept
1. Registered signer `S` is assigned `slot_id = 3` in both StackerDB contracts `A` (`.signers-0-1`) and `B` (`.signers-0-2`), which is possible because both contracts derive their `stackerdb-get-signer-slots` ordering from the same underlying signer set.
2. `S` signs a legitimate chunk for `A`: `StackerDBChunkData { slot_id: 3, slot_version: 1, data: D }.sign(S_privkey)`, producing signature `sig`.
3. An observer (or `S` itself) constructs `StackerDBChunkData { slot_id: 3, slot_version: 1, data: D, sig }` and submits it (via `StackerDBPushChunk` gossip or the `POST /v2/stackerdb/.../chunks` RPC) targeting contract `B` instead of `A`.
4. `StackerDBs::validate_received_chunk` for contract `B` calls `get_slot_signer(B, 3)` → `S`'s address, then `slot_metadata.verify(&S_addr)`, which succeeds because `auth_digest()` never included `A` or `B` in the hash — [3](#0-2)  — so the chunk is accepted into `B`'s slot 3 store and re-broadcast to `B`'s replicas as authentic topic-`B` data, even though it was produced and intended only for topic `A`.

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

**File:** stackslib/src/net/stackerdb/config.rs (L205-243)
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

```

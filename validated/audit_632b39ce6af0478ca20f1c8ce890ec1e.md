Confirmed: `try_replace_chunk` passes `smart_contract` only to look up the DB row and the slot's designated signer, but the actual cryptographic check (`slot_desc.verify(&slot_validation.signer)`) calls `SlotMetadata::verify`/`auth_digest`, which never mixes in the `QualifiedContractIdentifier`. No other layer (`poststackerdbchunk.rs`, `relay.rs`) reintroduces contract binding before calling `try_replace_chunk`.

### Title
Cross-contract StackerDB chunk replay due to missing contract-identifier binding in `SlotMetadata::auth_digest` - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest` (and therefore `StackerDBChunkData::sign`/`verify`/`recover_pk`) hashes only `slot_id`, `slot_version`, and `data_hash`, omitting the `QualifiedContractIdentifier` of the StackerDB the chunk belongs to. Because `StackerDBTx::try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs` only checks that the recovered signer address matches the slot's assigned signer *for that contract* — via `slot_desc.verify(&slot_validation.signer)` — but never checks that the signature itself commits to the contract, the exact same wire bytes (`slot_id`, `slot_version`, `sig`, `data`) signed for StackerDB contract A validate against StackerDB contract B whenever the same address is assigned the same `slot_id` in contract B.

### Finding Description
The intended equality is `signature_authenticates(contract_id, slot_id, version, data_hash)`, but the code actually only enforces `signature_authenticates(slot_id, version, data_hash)`: [1](#0-0) 

`try_replace_chunk` resolves the per-contract signer address via `get_slot_validation(smart_contract, slot_id)` and then calls `slot_desc.verify(&slot_validation.signer)`, but `verify`/`auth_digest` never take `smart_contract` as input: [2](#0-1) 

The public POST endpoint `/v2/stackerdb/:addr/:contract/chunks` accepts a JSON `StackerDBChunkData` and passes it straight to `try_replace_chunk` without any contract-binding check: [3](#0-2) 

Exploit flow: an attacker (or any observer, since StackerDB chunks are broadcast/relayed as public gossip and served via GET endpoints) captures a validly-signed `StackerDBChunkData` published for contract A slot 2 by address `S`. If `S` is also assigned slot 2 in a different StackerDB contract B (a realistic scenario, e.g. the same signer set/address participates in multiple `.signers-*` StackerDB contracts across cycles, or any two StackerDB contracts sharing overlapping slot-to-signer assignment), the attacker POSTs the identical bytes to contract B's chunks endpoint. `try_replace_chunk` for contract B looks up `slot_validation.signer == S`, calls `slot_desc.verify(&S)`, which recomputes `auth_digest` from `(slot_id=2, slot_version, data_hash)` only — identical to what was hashed for contract A — so the ECDSA recovery succeeds and the write is accepted into contract B as long as `slot_version` exceeds B's current version and does not exceed `max_writes`. This is a genuine domain-separation failure: a signature intended for one Clarity contract's StackerDB is accepted as authentic for a completely different contract's StackerDB, without the signer having authorized (signed) anything for contract B specifically.

### Impact Explanation
This allows an unauthenticated third party to cause data that was never signed for StackerDB contract B to be written into contract B's slot 2, forged from B's perspective (B never received a signature scoped to it), and then relayed/gossiped network-wide via the `StackerDBPushChunk` relay path triggered on `ack_resp.accepted`. This is an unauthenticated/unauthorized write to StackerDB state and network-wide propagation of forged data, matching the Critical category.

### Likelihood Explanation
Preconditions: the attacker needs no special privilege — StackerDB chunk data (including `sig`) is public (served via GET `/v2/stackerdb/...` and relayed over the p2p network), so capturing a valid `StackerDBChunkData` blob requires no secret. The attacker also needs two StackerDB contracts to exist where the same address is assigned to the same `slot_id`, which is plausible given how Stacks StackerDBs are configured per reward cycle/signer set with slots numbered from 0. Given that, the exploit is a single crafted HTTP POST, fully repeatable for every overlapping slot/version, and requires only remote RPC reachability.

### Recommendation
Bind the `QualifiedContractIdentifier` into `SlotMetadata::auth_digest` (e.g., hash `contract_id.issuer`, `contract_id.name`, `slot_id`, `slot_version`, `data_hash`), and thread the `smart_contract` identifier through `SlotMetadata::sign`/`verify`/`recover_pk` and all call sites (`StackerDBChunkData::sign`/`verify`, `try_replace_chunk`), so a signature is cryptographically scoped to exactly one StackerDB contract.

### Proof of Concept
Rust test (in `stackslib/src/net/stackerdb/tests/db.rs` style):
1. Create two StackerDB contracts A and B in a `StackerDBs::connect_memory()` instance via `create_stackerdb`, each assigning slot 2 to the same `StacksAddress` derived from a single `StacksPrivateKey`.
2. Build a `StackerDBChunkData::new(2, 1, data)` and call `.sign(&privkey)`, producing `sig` bound only to `(2, 1, data_hash)`.
3. Call `tx.try_replace_chunk(&contract_a_id, &chunk.get_slot_metadata(), &chunk.data)` — assert `Ok(())`.
4. Without re-signing, call `tx.try_replace_chunk(&contract_b_id, &chunk.get_slot_metadata(), &chunk.data)` with the identical `SlotMetadata`/`sig` — assert this also returns `Ok(())` (or equivalently, POST the identical JSON body to `/v2/stackerdb/:addr/:contractB/chunks` and assert `accepted: true` in `poststackerdbchunk.rs`), demonstrating the chunk intended for contract A is accepted as authentic for contract B.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L169-201)
```rust
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
        let http_peer = node.http_peer_addr();

        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

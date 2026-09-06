### Title
Cross-contract StackerDB chunk signature replay due to missing contract binding in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` hashes only `slot_id`, `slot_version`, and `data_hash`, omitting the target StackerDB's `contract_id`. Consequently, a chunk signature that is valid for slot `N` in one contract is also valid for slot `N` in any other contract as long as the address that legitimately owns slot `N` happens to be the same in both, allowing that already-published chunk to be replayed/stored under a different contract's DB.

### Finding Description
`SlotMetadata::auth_digest` computes the signed digest as:
```rust
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
``` [1](#0-0) 

This digest is what `SlotMetadata::sign`/`verify` operate on, and `StackerDBChunkData::verify`/`recover_pk` simply delegate to it via `get_slot_metadata().auth_digest()`. [2](#0-1) 

The verification/storage path in `StackerDBTx::try_replace_chunk` looks up the legitimate signer for the *target* contract's slot (`slot_validation.signer`, obtained from `get_slot_validation(smart_contract, slot_desc.slot_id)` scoped to that specific `smart_contract`), then calls `slot_desc.verify(&slot_validation.signer)`, which recovers the pubkey from the digest above and checks it hashes to that signer's address — with no check that the signature was produced for *this* `smart_contract` at all:
```rust
let slot_validation = self.get_slot_validation(smart_contract, slot_desc.slot_id)?...
if !slot_desc.verify(&slot_validation.signer)? { ... }
``` [3](#0-2) 

Because `contract_id` never enters the digest, if the same address `A` is the legitimate slot-`N` signer in contract `X` and also the legitimate slot-`N` signer in contract `Y`, a chunk `(slot_id=N, slot_version=V, sig, data)` that `A` signed and published to `X` will pass `verify()` verbatim when submitted to `Y`'s slot `N`, provided `V` is greater than `Y`'s current version for slot `N` and does not exceed `Y`'s `max_writes`. The remote, unprivileged entry point is `POST /v2/stackerdb/{address}/{contract}/chunks`, handled by `RPCPostStackerDBChunkRequestHandler::try_handle_request`, which parses the JSON body into a `StackerDBChunkData`, and calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), &stackerdb_chunk.data)` using only the `contract_identifier` taken from the URL path — the signature itself carries no contract binding to cross-check against. [4](#0-3)  On acceptance, the node also relays the replayed chunk network-wide via `StackerDBPushChunk`. [5](#0-4) 

An attacker who is an unprivileged network participant (no keys, no RPC secret) can simply observe/capture a validly-signed chunk gossiped/posted for contract `X` (StackerDB traffic is unauthenticated broadcast data by design) and re-POST the identical `(slot_id, slot_version, sig, data)` tuple to a different contract `Y`'s `/chunks` endpoint on any node. No signing key is needed because the same bytes and signature are reused verbatim.

### Impact Explanation
This allows unauthorized cross-contract writes to a StackerDB slot: data that a signer authorized only for contract `X` is stored as if authorized for contract `Y`, and it will be relayed/gossiped network-wide as a legitimate `Y`-slot update via `StackerDBPushChunk`. This matches the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" Critical category, since the write into `Y`'s slot did not actually receive authorization scoped to `Y`.

### Likelihood Explanation
Exploitation requires: (1) the same address is assigned slot `N` in two different StackerDB contracts (common in practice, since signer-set/slot assignment is often derived deterministically from stacking rank across consecutive reward cycles, making address-to-slot-index reuse across contract instances plausible), and (2) the attacker can observe a legitimately signed chunk for one of those contracts (trivial, since StackerDB chunks are broadcast/relayed data, and the POST/GET StackerDB RPC endpoints are unauthenticated). The attacker needs no private key and no privileged role — only the ability to capture one broadcast chunk and re-POST it to a different contract's endpoint, which is fully remote and repeatable for every future version bump of that chunk.

### Recommendation
Bind the digest to the specific StackerDB contract by including the `contract_id` (e.g., `QualifiedContractIdentifier` serialized bytes) in `SlotMetadata::auth_digest`, and update the signing/verification round-trip (`sign`, `verify`, `StackerDBChunkData::sign/verify/recover_pk`) plus `try_replace_chunk`/callers to pass the contract id through so the digest computed matches the exact `smart_contract` being written to.

### Proof of Concept
In `libstackerdb/src/libstackerdb.rs`'s test module (or a new `stackslib` integration test), construct two `QualifiedContractIdentifier`s `contract_a` and `contract_b`, create two in-memory `StackerDBs` (or slots within a shared `StackerDBs::connect_memory()` for each contract) both assigning the same `StacksAddress` (derived from one `StacksPrivateKey`) to `slot_id = 0`. Build a `StackerDBChunkData::new(0, 1, data)` and `sign(&privkey)`; call `try_replace_chunk(&contract_a, &chunk.get_slot_metadata(), &chunk.data)` and assert `Ok(())`. Then, without re-signing, call `try_replace_chunk(&contract_b, &chunk.get_slot_metadata(), &chunk.data)` and assert it also returns `Ok(())` — demonstrating the identical signature/chunk accepted for a different contract's slot, which should instead fail with `BadSlotSigner`/`VerifyingError` once the digest is contract-scoped.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
        }
```

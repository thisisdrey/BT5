### Title
Cross-contract StackerDB chunk replay via contract-agnostic signature digest - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest` computes the signed digest only from `slot_id`, `slot_version`, and `data_hash`, never binding the signature to a specific StackerDB contract. `StackerDBTx::try_replace_chunk` verifies the signature against the target contract's stored `signer` address only, so a chunk legitimately signed for slot N of contract A can be replayed verbatim against slot N of contract B if both contracts assign the same signer address to that slot.

### Finding Description
`SlotMetadata::auth_digest` hashes `slot_id.to_be_bytes()`, `slot_version.to_be_bytes()`, and `data_hash.0` only [1](#0-0) . `SlotMetadata::verify` recovers the pubkey from this digest and the signature, then checks the resulting `Hash160` against the caller-supplied `principal` [2](#0-1) . The contract identifier is never part of what's hashed or signed.

`StackerDBTx::try_replace_chunk` looks up `slot_validation.signer` for `(smart_contract, slot_id)` from the SQL `chunks` table and calls `slot_desc.verify(&slot_validation.signer)` [3](#0-2) . This ties the verification to the *target* contract's configured signer for that slot, but that's exactly the false equality: `signature-verifies-for-contract-B == signature-was-produced-for-contract-A`, because the signed bytes never encoded contract B (or A) at all — only `(slot_id, slot_version, data_hash)`.

`RPCPostStackerDBChunkRequestHandler::try_handle_request` parses the target contract from the URL path (`/v2/stackerdb/{address}/{contract}/chunks`) and the chunk body independently, then calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), ...)` [4](#0-3) . Nothing here cross-checks that the chunk was produced for this specific contract.

Exploit flow:
1. Attacker legitimately owns slot 0 in StackerDB contract A (their address is `signer` for slot 0 of A) and also happens to be the configured signer of slot 0 in contract B (e.g., same signer set reused for another reward cycle's StackerDB, which is a normal/likely deployment pattern).
2. Attacker builds a `StackerDBChunkData{slot_id: 0, slot_version: V, data}`, signs it — the signature is valid over `(0, V, sha512_256(data))`, independent of which contract it targets.
3. Attacker POSTs this exact tuple `(slot_id, slot_version, sig, data)` to `/v2/stackerdb/<addrB>/<contractB>/chunks`.
4. `try_replace_chunk` fetches contract B's slot 0 `signer` (attacker's own address, since they legitimately own slot 0 there too, or even any signer whose key the attacker controls under both contracts) and successfully verifies the signature because the digest never encoded which contract it was for.
5. Version check only compares against contract B's own current stored version for slot 0, which the attacker can always satisfy by picking `V` above whatever B's current version is.
6. The chunk is written to contract B's database and, since insertion is treated as accepted, is relayed to the network via `StacksMessageType::StackerDBPushChunk` [5](#0-4) , causing it to propagate node-wide as authentic data for contract B.

Existing guards that fail to stop this: the `slot_desc.verify(&slot_validation.signer)` check does bind to an *address*, but that address is a property shared across contracts when the same signer set is reused (e.g., across reward cycles for signer-set StackerDBs), so it does not bind to the *contract*. The chunk-size and version/staleness checks [6](#0-5)  do not address contract binding either.

### Impact Explanation
An attacker who legitimately controls a StackerDB slot in one contract can forge/replay writes into any other StackerDB contract where the same address happens to be assigned the same slot ID — without holding any key or privilege specific to that second contract. This is an unauthenticated cross-contract write to StackerDB state that gets committed to the SQL `chunks` table and relayed network-wide as a legitimate `StackerDBPushChunk`, matching the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" Critical impact category. This is repeatable for every version bump and works against any contract-slot pair sharing a signer.

### Likelihood Explanation
Preconditions: two StackerDB contracts (e.g. successive reward-cycle signer DBs, or any two contracts whose slot layouts assign the same address to the same `slot_id` — a realistic and even common configuration since signer sets are often reused across cycles) must exist, and the attacker must hold the private key for that shared address on at least one of them (which they do, as owner of the source slot). No RPC secret or admin role is needed — the POST endpoint is reachable by any remote peer via the node's public RPC surface. Attacker cost is minimal: sign once, replay the bytes with a different URL path.

### Recommendation
Include the `QualifiedContractIdentifier` (or its serialized string) in `SlotMetadata::auth_digest` before hashing, so the signature commits to the specific StackerDB contract as well as `(slot_id, slot_version, data_hash)`. This requires threading the contract identifier into `SlotMetadata::sign`/`verify`/`auth_digest`, updating all call sites (`StackerDBChunkData::sign`, `get_slot_metadata`, `recover_pk`, `verify`) to pass the contract context, and bumping the wire format/version if backward compatibility with old signed chunks must be preserved.

### Proof of Concept
Rust test plan in `stackslib::net::stackerdb::tests::db`:
1. Create two `StackerDBs` databases in-memory via `StackerDBs::connect_memory()`.
2. Call `create_stackerdb` for contract A and contract B, each with slot 0 assigned to the same `StacksAddress` derived from a single `StacksPrivateKey`.
3. Build a `StackerDBChunkData { slot_id: 0, slot_version: 1, data: b"forged".to_vec(), sig: MessageSignature::empty() }`, call `.sign(&privkey)` (this signs only `(0, 1, hash(data))`, contract-agnostic).
4. Call `tx.try_replace_chunk(&contract_id_A, &chunk.get_slot_metadata(), &chunk.data)` — expect `Ok(())`.
5. Call `tx.try_replace_chunk(&contract_id_B, &chunk.get_slot_metadata(), &chunk.data)` using the *same* unmodified `chunk`/signature — assert it also returns `Ok(())`, proving the chunk signed for contract A is accepted as valid for contract B.
6. Confirming assertion: `assert!(dbB.get_chunk(&contract_id_B, 0, 1).unwrap().is_some())` shows contract B's slot 0 now holds attacker-forged data written under a signature never produced with contract B in mind.

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

**File:** libstackerdb/src/libstackerdb.rs (L183-193)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L406-436)
```rust
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
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
        if slot_desc.slot_version > self.config.max_writes {
            return Err(net_error::TooManySlotWrites {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
                max_writes: self.config.max_writes,
            });
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

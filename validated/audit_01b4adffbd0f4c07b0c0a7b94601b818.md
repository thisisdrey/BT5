### Title
StackerDB Chunk Signatures Omit Contract/Instance Binding, Enabling Cross-Contract Replay - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest from only `slot_id`, `slot_version`, and `data_hash` [1](#0-0) . It omits any binding to the specific StackerDB smart-contract instance (or chain/network identifier) that the chunk was authorized for. Any unprivileged network peer that observes a validly-signed chunk (via StackerDB chunk gossip, `StackerDBChunkInv`/push, or the public chunk-fetch RPC) can replay that exact `(slot_id, slot_version, sig, data)` tuple against a *different* StackerDB contract instance, and it will be accepted as valid there as long as the same signer address happens to own that `slot_id` in the target contract and the target contract's current recorded version for that slot is `<=` the replayed version.

### Finding Description
Chunk authentication is split across two independent checks that never verify each other cross-consistently:

1. `StackerDBChunkData::verify`/`SlotMetadata::verify` recovers the public key from the signature over `auth_digest()` = `H(slot_id || slot_version || data_hash)` and hashes it to a `StacksAddress` [2](#0-1) .
2. Whether that recovered address is *authorized* is looked up per-contract via `get_slot_signer(smart_contract_id, slot_id)` inside `StackerDBs::validate_received_chunk` [3](#0-2) , and again in the SQL-backed `try_replace_chunk` write path (`stackslib/src/net/stackerdb/db.rs`).

The `smart_contract_id` parameter used for step 2 is supplied out-of-band by the request/gossip envelope; it is never part of the bytes that were actually signed in step 1. Consequently, the signature does not attest to *which* StackerDB contract it was intended for — only to a `(slot_id, slot_version, data_hash)` triple. If the same signer address is assigned the same `slot_id` in two different StackerDB contract instances — which is architecturally likely for `signer-<reward-cycle>-<message-id>` boot contracts, since the same underlying signer set (and therefore likely the same address→slot_id assignment) is reused across different `message-id` "lanes" for a given reward cycle (see the lane dispatch in `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` at `libsigner/src/events.rs:568-620`, which treats `signer-XXX-YYY` contracts as parallel lanes over the same signer set) — a chunk captured from lane A can be re-submitted (e.g. via `POST /v2/stackerdb/{contract}/chunks`) to lane B and will pass both `validate_received_chunk` and `try_replace_chunk` there, because the version/staleness and max-writes checks are local to lane B's own slot-version counter and independent of the contract the chunk was originally authorized for [4](#0-3) .

This is the direct structural analog of the reported `QuestFactory.mintReceipt` bug: there, the signed message committed only to `(msg.sender, questId_)` and omitted `chainid`/contract address, allowing replay across different `QuestFactory` deployments; here, the signed digest commits only to `(slot_id, slot_version, data_hash)` and omits the StackerDB contract identifier, allowing replay across different StackerDB contract instances that happen to share slot/address assignment.

### Impact Explanation
A successful replay causes a signer's own previously-broadcast chunk (of stale/wrong semantic type for the target lane, e.g. a `BlockResponse` payload replayed into a lane expecting `BlockProposal`-adjacent content, or an old reward-cycle message replayed into a newer cycle's contract) to be accepted and stored as if freshly and correctly authored for the target contract, i.e., **unauthorized/unintended write to StackerDB state under someone else's implied authorization scope**, and it will then be propagated network-wide via normal StackerDB chunk-inventory sync/push (`StackerDBSync`/`handle_unsolicited_StackerDBPushChunk`), causing forged/misattributed data to spread to all replicating peers. Downstream consumers such as `stacks-signer` decode chunks per-lane based on the type-prefix byte inside the data (`signer_message_payload_matches_lane` in `libsigner/src/events.rs:584-595`), which provides some filtering, but this filter is advisory at the event-consumption layer only — it does not prevent the chunk from being durably stored and propagated at the StackerDB layer itself.

### Likelihood Explanation
Exploitation requires no privileged access — any network peer can observe a broadcast chunk and re-POST it to another contract endpoint (unauthenticated, unprivileged, standard chunk-push/RPC paths already reachable in scope). However, it is conditioned on a specific alignment: the same signer address must own the identical `slot_id` in both the source and target StackerDB contracts, and the target's recorded slot version for that slot must not already exceed the replayed version. This condition is plausible (not merely theoretical) for `signer-<cycle>-<message-id>` lanes sharing one reward cycle's signer set, but is not guaranteed to hold generally across arbitrary contract pairs, mirroring the original report's own "Medium" severity determination (which was downgraded from High specifically because the analogous precondition — matching `(id, signer)` pairs across instances — was not always satisfied).

### Recommendation
Bind the signed digest to the specific StackerDB instance and, ideally, the network: include the `QualifiedContractIdentifier` (or an internal `stackerdb_id`) and `network_id`/`chain_id` in `SlotMetadata::auth_digest()` before signing/verifying, e.g. `H(contract_id || slot_id || slot_version || data_hash)`, and update `StackerDBChunkData::sign`/`verify`/`recover_pk` accordingly. This mirrors the original recommendation of embedding contract address (and chain id) into the signed payload.

### Proof of Concept
Conceptual PoC (not executable against the index, since it requires live P2P/StackerDB fixtures already exercised by `stackslib/src/net/tests/relay/nakamoto.rs`):
1. Configure two StackerDB contracts, A and B, both with the same signer address assigned to `slot_id = 0` (e.g. representative of `signer-N-0` and `signer-N-1` sharing one reward cycle's signer set).
2. Signer signs a chunk for contract A: `StackerDBChunkData::new(0, 1, data).sign(&privk)` — this signs only `(0, 1, hash(data))`, per `auth_digest()` [1](#0-0) .
3. An observer captures this chunk from contract A's gossip/push traffic.
4. Observer re-submits the identical chunk bytes to contract B's chunk-push/RPC endpoint. `validate_received_chunk` for contract B independently resolves the same address for slot 0 via `get_slot_signer(contract_B_id, 0)` and calls `slot_metadata.verify(&addr)`, which succeeds because the digest never encoded which contract it belonged to [3](#0-2) .
5. The chunk is accepted, stored, and propagated in contract B despite never having been authorized for contract B.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

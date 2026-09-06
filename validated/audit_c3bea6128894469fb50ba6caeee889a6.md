This confirms the finding. The `auth_digest()` in `SlotMetadata` only commits to `slot_id`, `slot_version`, and `data_hash` [1](#0-0) , and `try_replace_chunk` validates the signature purely against the slot's recorded signer and version, with no binding to the target `smart_contract` (StackerDB contract identifier) anywhere in the signed payload or the check itself [2](#0-1) .

### Title
Cross-StackerDB Signature Replay Enables Unauthorized Chunk Overwrite - (File: `stackslib/src/net/stackerdb/db.rs`, `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest as `hash(slot_id || slot_version || data_hash)` only [1](#0-0) . It never incorporates the target StackerDB's `smart_contract` (`QualifiedContractIdentifier`) or any other domain-separation value. `StackerDBTx::try_replace_chunk` verifies the signature only against the recorded `slot_validation.signer` for the given `smart_contract`/`slot_id` pair, and enforces only a monotonic version check (`slot_desc.slot_version <= slot_validation.version` → `StaleChunk`) — it never checks that the signature was produced for *this* contract [3](#0-2) . The same fault exists in the p2p validation path, `PeerNetwork::validate_received_chunk`, which likewise verifies only signer + version and never the contract identifier [4](#0-3) .

### Finding Description
This is the same bug class as the external report: a signature that authenticates `(action, data)` but omits a domain/context binding (there, `address(this)`/`chain.id`; here, the StackerDB contract identifier) can be replayed outside its intended context. StackerDB chunks are already publicly broadcast/gossiped between nodes and served over the public `POST /v2/stackerdb/{principal}/{contract_name}/chunks` RPC endpoint (`RPCPostStackerDBChunkRequestHandler`) [5](#0-4) , so any remote, unprivileged observer can capture a validly-signed `(slot_id, slot_version, data_hash)` tuple + signature from StackerDB A.

If the same signer address owns the same `slot_id` in a different StackerDB instance B (which is common in practice: the Stacks signer set reuses the same private keys and typically the same relative slot ordering across different `.signers-<cycle>-<n>` reward-cycle contracts, and across other node-configured replicas such as miner-coordination StackerDBs), the captured signature+chunk is fully valid for insertion into B as well, provided B's current recorded `version` for that slot is `< slot_desc.slot_version`. Neither `try_replace_chunk` nor `validate_received_chunk` reject this because the equality that should be enforced — "signature was produced for *this* StackerDB contract" — is never checked; only "signature recovers to the recorded signer of this slot" is checked, and the signer's public key hash is identical across contracts.

### Impact Explanation
This breaks the "authenticated-for-target vs. stored-in-target" equality for StackerDB writes: it is an unauthorized/unauthenticated write of attacker-replayed (stale but validly-signed) data into a StackerDB that the signer never intended to update. Depending on which StackerDB is targeted, this can revert or desynchronize replicated signer-coordination/message state (e.g. resurrect an old, no-longer-current signer message into a different reward cycle's replica if slot/version alignment permits), which maps to "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" once other nodes replicate the replayed chunk from the ingesting node.

### Likelihood Explanation
Exploitation requires no privileged access and no cryptographic break — only observation of legitimately broadcast/public chunk data (trivial, since StackerDB data is designed to propagate widely) and a target StackerDB where the same signer address occupies the same `slot_id` with a lower recorded version. This precondition is plausible given how Stacks signer StackerDB contracts are provisioned per reward cycle with often-stable slot assignments for the same signer keys, but it is a precondition, not guaranteed for arbitrary contract pairs, which somewhat bounds the likelihood.

### Recommendation
Include the target contract identifier (and/or a `chain-id`/network identifier) in `SlotMetadata::auth_digest()`, e.g. `hash(smart_contract_id || slot_id || slot_version || data_hash)`, mirroring the report's EIP-712-style recommendation of binding signatures to `address(this)`/`chain.id`. This requires updating `SlotMetadata::sign`/`verify`, `StackerDBChunkData`, and all callers (`try_replace_chunk`, `validate_received_chunk`, RPC handlers, and `stacks-signer` chunk construction) to pass the contract identifier into the signed digest.

### Proof of Concept
1. Signer `S` owns `slot_id = 0` in StackerDB contract `A` and also `slot_id = 0` in StackerDB contract `B` (both currently at version 0 or lower than the captured version).
2. `S` legitimately signs and posts a chunk to `A`: `SlotMetadata{slot_id:0, slot_version:5, data_hash:H}` with signature `sig` via `POST /v2/stackerdb/.../A/chunks`.
3. Attacker observes this broadcast chunk (public P2P StackerDB gossip or the public RPC response) and replays the identical `(slot_id, slot_version, data_hash, sig, data)` tuple to `POST /v2/stackerdb/.../B/chunks`.
4. `RPCPostStackerDBChunkRequestHandler::try_handle_request` calls `try_replace_chunk` for contract `B` [5](#0-4) ; `slot_desc.verify(&slot_validation.signer)` succeeds because `S`'s address matches the recorded signer for slot 0 in `B` too, and `slot_desc.slot_version(5) > slot_validation.version` in `B`, so the chunk is accepted and stored in `B` — despite `S` never having signed anything intended for `B`.

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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
        self.insert_chunk(smart_contract, slot_desc, chunk)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-706)
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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

### Title
StackerDB `write_freq` rate limit is never enforced on the chunk-write path, allowing a slot owner to bypass the intended write throttle and trigger unbounded network-wide broadcast - (File: `stackslib/src/net/stackerdb/db.rs`, `stackslib/src/net/api/poststackerdbchunk.rs`, `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`StackerDBConfig.write_freq` is the wall-clock rate limit that is supposed to bound how often a slot's owner may write a new chunk. It is documented as an enforced control, but the actual storage function that mutates state, `StackerDBTx::try_replace_chunk`, only checks staleness and the `max_writes` counter — it never checks `write_freq`. The two real ingestion paths into that function (the unsolicited P2P push handler and the unauthenticated-by-role HTTP POST endpoint) also never check it before calling it. As a result, any party who legitimately owns a StackerDB slot signing key can submit chunks arbitrarily fast, and every accepted chunk is immediately re-broadcast to the whole P2P network, defeating the purpose of the rate limit and producing an amplification vector.

### Finding Description
The write function is explicit about not enforcing the rate limit: [1](#0-0) 

It only validates chunk size, slot ownership signature, staleness, and `max_writes` — `write_freq` (a wall-clock cooldown) is absent from this function entirely: [2](#0-1) 

The push-chunk validation routine used for P2P-received chunks likewise omits any `write_freq`/wall-clock check — it only checks chunk size, expected version/signer, staleness, and `max_writes`: [3](#0-2) 

The comment directly above this function acknowledges the omission is deliberate for the P2P push path ("The write frequency is not checked for this chunk...") on the theory that `ConversationP2P` bandwidth-throttling covers it: [4](#0-3) 

However, the same unthrottled `try_replace_chunk` function is also the single write path used by the RPC endpoint `RPCPostStackerDBChunkRequestHandler::try_handle_request`, which has no bandwidth-throttling equivalent and no `write_freq` check of its own: [5](#0-4) 

Critically, every chunk accepted through this endpoint is immediately queued for network-wide relay, regardless of how quickly it followed the previous write from the same slot: [6](#0-5) 

This mirrors the reported bug class exactly: a policy value meant to bound a costly, per-identity operation (`MaxCompletions` in the report / `write_freq` here) is enforced nowhere near the actual state-mutating/resource-consuming function, so the party who legitimately controls the credential (a valid slot signer, analogous to the questing account) can trigger the expensive downstream action (VRF request / network-wide chunk broadcast) far more often than the design intends, simply by hitting the exposed write path directly instead of whatever path the designers assumed would apply the throttle.

### Impact Explanation
Every chunk write accepted via the HTTP POST endpoint results in a `StackerDBPushChunk` message being relayed to a sampled set of P2P peers, each of which will re-verify the signature, store the chunk, and further re-broadcast it. Because `write_freq` is not enforced on this path, a slot owner can submit new signed chunks (each just bumping `slot_version`) as fast as HTTP requests can be issued (bounded only by `max_writes`, which can be configured very high or effectively unlimited), causing unbounded compute (signature verification, DB writes) and bandwidth amplification across the network from a single legitimately-signed but abusive sender. This falls under "bounded compute DoS on a read/write endpoint" and network-wide propagation amplification from a single slot's credential, which is a High-impact class per the intended control's purpose.

### Likelihood Explanation
Exploitability requires only possession of a valid private key for a slot the StackerDB config already assigns write access to (e.g., a `.signers` or naming StackerDB participant) — no other privilege, admin role, or third-party key is needed. The endpoint is a standard, always-available RPC route, and the omission is structural/consistent across both P2P push validation and the RPC handler, so it can be reliably triggered.

### Recommendation
Enforce `StackerDBConfig.write_freq` inside `StackerDBTx::try_replace_chunk` (or immediately before it is called from both `handle_unsolicited_StackerDBPushChunk`/`validate_received_chunk` and `RPCPostStackerDBChunkRequestHandler::try_handle_request`), using the slot's persisted `write_time` versus current time, returning a distinct rejection (e.g., a `TooFrequentWrites` error/ack code) when a write arrives before the cooldown elapses.

### Proof of Concept
1. Obtain (or be) a legitimate StackerDB slot signer for a StackerDB contract configured with a non-trivial `write_freq` (e.g., 60s) intended to cap write rate.
2. Repeatedly call `POST /v2/stackerdb/{address}/{contract}/chunks` with a freshly-signed `StackerDBChunkData` each time, incrementing `slot_version` by 1 and keeping the payload within `chunk_size`.
3. Observe that `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-437`) accepts every request as long as `slot_version` increases and stays under `max_writes`, with no delay enforced, and that each accepted write is relayed via `node.set_relay_message` (`stackslib/src/net/api/poststackerdbchunk.rs:315-324`), causing the node to broadcast a `StackerDBPushChunk` to the network on every single request — far more frequently than `write_freq` was intended to allow.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L398-416)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L418-437)
```rust
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

**File:** stackslib/src/net/stackerdb/mod.rs (L731-734)
```rust
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-208)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
                    test_debug!(
                        "Failed to replace chunk {}.{} in {}: {:?}",
                        stackerdb_chunk.slot_id,
                        stackerdb_chunk.slot_version,
                        &contract_identifier,
                        &e
                    );
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

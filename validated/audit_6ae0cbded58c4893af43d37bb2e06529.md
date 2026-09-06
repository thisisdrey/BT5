## Title
`write_freq` rate-limit is never enforced on the StackerDB write path, allowing slot owners to bypass the per-slot write-frequency cap - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The external report describes a threshold check (`_minimumTaintedTransferAmount`) that is enforced on one path but can be trivially bypassed by routing the same operation through a different path that omits the check. The stacks-core StackerDB subsystem has the same structural flaw: `StackerDBConfig::write_freq` ("minimum wall-clock time between writes to the same slot") is enforced only on the *pull/fetch* decision path, but is never checked on the *write/store* path that actually persists a chunk.

### Finding Description
`StackerDBConfig.write_freq` is documented as the minimum wall-clock time between writes to the same slot: [1](#0-0) 

This value is only consulted in `StackerDBSync::get_chunks_to_fetch`, when the node decides whether it should *proactively pull* a chunk from a peer: [2](#0-1) 

However, the function that actually validates and accepts a pushed/uploaded chunk, `PeerNetwork::validate_received_chunk`, explicitly documents that it skips this check: [3](#0-2) 

And the authoritative state-mutating function, `StackerDBTx::try_replace_chunk` — used both by the unauthenticated-content-but-signature-checked HTTP `POST /stackerdb/.../chunk` handler and by unsolicited P2P `StackerDBPushChunk` handling — checks chunk size, slot signer, staleness, and `max_writes`, but never checks `write_freq` at all: [4](#0-3) 

This is called directly from the HTTP chunk-upload RPC handler with no `write_freq` gate anywhere in between: [5](#0-4) 

So exactly as in the report's pattern (a limit enforced in one place but bypassable via another path to the same effect), any legitimate StackerDB slot owner (someone who legitimately holds a private key for one of the `signers` slots registered in the DB's control smart contract) can submit new, validly-signed, monotonically-versioned chunks as fast as they want over HTTP or P2P push, completely ignoring the wall-clock `write_freq` throttle that the protocol/config intends to impose.

### Impact Explanation
`write_freq` exists specifically to bound how often any given slot can be rewritten, in order to bound disk I/O, storage churn, and — critically — StackerDB replication/gossip traffic, since every successful write causes `StackerDBChunkInv` updates to be pushed/relayed to all replicating peers network-wide. Bypassing it lets an authorized-but-unprivileged slot owner (e.g., a registered signer under a StackerDB contract) drive unbounded write and gossip amplification across every replicating node, well beyond what the protocol's designed rate limit permits.

### Likelihood Explanation
High: no special access is needed beyond already holding a legitimate slot-signing key for a StackerDB (a normal application-level permission, not an admin role or the node's secret key). The attacker simply calls the standard chunk-upload RPC or push-chunk message repeatedly with incrementing `slot_version` values — both fully supported, unauthenticated-at-the-network-layer code paths.

### Recommendation
Enforce `write_freq` inside `StackerDBTx::try_replace_chunk` (and/or `validate_received_chunk`) by checking the previous `write_time` for the slot against `now`, mirroring the check already done in `sync.rs`, so the invariant is enforced uniformly on every path that can mutate a slot, not only on the peer-selection heuristic used when deciding whether to actively fetch data.

### Proof of Concept
1. Configure a StackerDB with `write_freq = N` seconds.
2. As a valid slot signer, sign and `POST` a chunk with `slot_version = v` to `/v2/stackerdb/<contract>/chunks/<slot>` (per `poststackerdbchunk.rs`) — accepted.
3. Immediately (within `< N` seconds) sign and `POST` another chunk for the same slot with `slot_version = v+1`.
4. Observe it is accepted by `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:398-439`) because only `chunk_size`, signer, staleness, and `max_writes` are checked — `write_freq` is never consulted — demonstrating the throttle is bypassable via the write RPC/push path even though `sync.rs` respects it on the pull path.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L232-247)
```rust
/// Settings for the Stacker DB
#[derive(Clone, Debug, PartialEq)]
pub struct StackerDBConfig {
    /// maximum chunk size
    pub chunk_size: u64,
    /// list of who writes and how many slots they have
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
    /// maximum number of times a slot may be written to during a reward cycle.
    pub max_writes: u32,
    /// hint for some initial peers that have replicas of this DB
    pub hint_replicas: Vec<NeighborAddress>,
    /// hint for how many neighbors to connect to
    pub max_neighbors: usize,
}
```

**File:** stackslib/src/net/stackerdb/mod.rs (L641-666)
```rust
    /// Validate chunk data either downloaded (with [`StackerDBSync::validate_downloaded_chunk`]), or
    /// pushed to us (with [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`])
    ///
    /// NOTE: does not check write frequency, since the caller has different ways of doing this.
    /// Returns:
    /// - Ok(true) if the chunk is valid
    /// - Ok(false) if the chunk is invalid
    /// - Err(..) on DB error
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
```

**File:** stackslib/src/net/stackerdb/sync.rs (L340-352)
```rust
            if self.write_freq > 0 && write_ts + self.write_freq > now {
                debug!(
                    "{:?}: {}: Chunk {} was written too frequently ({} + {} > {}) in {}, so will not fetch chunk",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    i,
                    write_ts,
                    self.write_freq,
                    now,
                    &self.smart_contract_id,
                );
                continue;
            }
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-439)
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
    }
}
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L179-201)
```rust
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

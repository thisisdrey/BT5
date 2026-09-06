### Title
StackerDB write-frequency (`write_freq`) rate limit is never enforced on chunk writes, allowing unbounded-rate chunk floods - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The external report describes a missing state/time check (`is_end_time_reached()`) before a privileged action is allowed to proceed. The closest verified analog in the in-scope repo is `StackerDBTx::try_replace_chunk()`, which is the sole authoritative gate for accepting a signed StackerDB chunk write, but it never checks the per-slot wall-clock `write_freq` configured for the StackerDB, even though that field exists specifically to bound how often a slot owner may write.

### Finding Description
`try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs` performs exactly four checks before calling `insert_chunk`: chunk size, signer verification, staleness (`slot_version` must increase), and `max_writes`. [1](#0-0) 

It never reads or compares against `slot_validation.write_time` versus `self.config.write_freq`, even though `SlotValidation` explicitly tracks `write_time` and `StackerDBConfig` explicitly carries `write_freq` as the "config-given wall-clock write frequency" per the test/doc comments. [2](#0-1) 

`insert_chunk` unconditionally stamps `write_time = get_epoch_time_secs()` on every accepted write, confirming the field is tracked for exactly this purpose, yet nothing reads it back for comparison against `write_freq` in the write path. [3](#0-2) 

This is the same fault pattern as the reported bug: an authoritative state-mutating function omits a time-based gate (`sale.is_end_time_reached()` ↔ `write_time + write_freq <= now`) that the surrounding code/config clearly intends to enforce, allowing the actor (a valid slot signer) to act (write a chunk) more often than the protocol's designed cadence permits.

The peer-to-peer path explicitly documents that write-frequency is intentionally *not* checked there because bandwidth throttling handles it, and defers enforcement to "the caller" — i.e., `try_replace_chunk`, which is the terminal DB-write gate for both the HTTP POST path and this deferred p2p case: [4](#0-3) [5](#0-4) 

The public, unauthenticated-per-connection HTTP endpoint `POST /v2/stackerdb/:address/:contract/chunks` calls `tx.try_replace_chunk(...)` directly and, on success, immediately relays the accepted chunk as a `StackerDBPushChunk` gossip message to the network: [6](#0-5) [7](#0-6) 

Because `try_replace_chunk` is the single choke point relied upon by both ingestion paths to enforce `write_freq`, and it does not do so, any holder of a slot's private key (any legitimately-registered StackerDB signer — e.g. a Nakamoto miner/signer using the signers StackerDB) can submit new, validly-signed, monotonically-incrementing-version chunks as fast as the network/API layer allows, well beyond the interval the contract-configured `write_freq` is meant to enforce.

### Impact Explanation
This breaks the intended equality "accepted write rate ≤ configured `write_freq`" for state that is subsequently gossiped network-wide via `StackerDBPushChunk`. A malicious-but-legitimate slot owner can use this to flood their own slot's chunk churn far faster than intended, forcing every replicating node to repeatedly re-verify signatures, re-hash chunk data, write to the local StackerDB SQLite store, and re-broadcast pushes to their StackerDB neighbor set — all triggered by valid signatures from a single legitimately-provisioned key. This is a bounded-compute amplification/DoS vector reaching every replica of that StackerDB (miners' and signers' StackerDBs are widely replicated), fitting the "bounded compute DoS on a read/write endpoint" and "network-wide propagation" impact classes, without requiring any secret beyond a key the actor is already entitled to hold as a slot owner.

### Likelihood Explanation
Likelihood is high: exploitation requires nothing more than a normal, permitted StackerDB signer submitting chunks back-to-back via the standard `POST /v2/stackerdb/.../chunks` API, incrementing `slot_version` each time (which is required and easy) — no special privilege escalation, no consensus-level identity forgery, and no unusual timing window is needed. The only constraint the honest protocol expects to apply (`write_freq`) is silently absent from the enforcement path.

### Recommendation
Add a check in `try_replace_chunk` (and/or in `validate_received_chunk` for the p2p acceptance path if desired) that rejects a chunk when `get_epoch_time_secs() < slot_validation.write_time + self.config.write_freq`, mirroring the existing `StaleChunk`/`TooManySlotWrites` error pattern (e.g., a new `net_error::WriteTooFrequent` variant), so the “write_freq” configuration this data structure clearly intends to support is actually enforced at the sole write gate.

### Proof of Concept
1. Configure any StackerDB contract with `write-freq: u120` (a 120-second minimum interval between writes to a slot), as is typical for production StackerDB configs.
2. As a registered slot signer, sign and submit chunk version 1 via `POST /v2/stackerdb/<address>/<contract>/chunks`; observe it is accepted and broadcast via `StackerDBPushChunk`.
3. Immediately (within the same second) sign and submit chunk version 2 for the same slot.
4. Observe `try_replace_chunk` accepts it (per `stackslib/src/net/stackerdb/db.rs:400-437`, none of the four checks reference `write_freq` or `write_time`), and the node relays it again — repeatable arbitrarily fast, in violation of the configured `write_freq`.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L374-396)
```rust
    fn insert_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "UPDATE chunks SET version = ?1, data_hash = ?2, signature = ?3, data = ?4, write_time = ?5 WHERE stackerdb_id = ?6 AND slot_id = ?7";
        let mut stmt = self.sql_tx.prepare(sql)?;

        let args = params![
            slot_desc.slot_version,
            Sha512Trunc256Sum::from_data(chunk),
            slot_desc.signature,
            chunk,
            u64_to_sql(get_epoch_time_secs())?,
            stackerdb_id,
            slot_desc.slot_id,
        ];

        stmt.execute(args)?;
        Ok(())
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

**File:** stackslib/src/net/stackerdb/tests/db.rs (L325-346)
```rust
/// Test that we can insert and query chunks to a StackerDB.
/// * verifies that they must be signed
/// * verifies that they mut not be stale
/// * verifies that they cannot exceed the config-given wall-clock write frequency
/// * verifies that they cannot exceed the per-chunk write count
#[test]
fn test_stackerdb_insert_query_chunks() {
    let path = "/tmp/test_stackerdb_insert_query_chunks.sqlite";
    setup_test_path(path);

    let sc = QualifiedContractIdentifier::new(
        StacksAddress::new(0x01, Hash160([0x01; 20]))
            .unwrap()
            .into(),
        ContractName::try_from("db1").unwrap(),
    );

    let mut db = StackerDBs::connect(path, true).unwrap();

    let mut db_config = StackerDBConfig::noop();
    db_config.max_writes = 3;
    db_config.write_freq = 120;
```

**File:** stackslib/src/net/stackerdb/mod.rs (L641-649)
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L720-741)
```rust
    /// Handle unsolicited StackerDBPushChunk messages.
    /// Check to see that the message can be stored or buffered.
    ///
    /// Optionally, make a reply handle for a StackerDBChunksInv to be sent to the remote peer, in which
    /// the inventory vector is updated with this chunk's data.  Or, send a NACK if the chunk
    /// cannot be buffered or stored.
    ///
    /// Note that this can happen *during* a StackerDB sync's execution, so be very careful about
    /// modifying a state machine's contents!  The only modification possible here is to wakeup
    /// the state machine in case it's asleep (i.e. blocked on waiting for the next sync round).
    ///
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
    /// Returns (true, x) if we should buffer the message and try processing it again later.
    /// Returns (false, x) if we should *not* buffer this message, because it either *won't* be valid
    /// later, or if it can be stored right now.
    ///
    /// Returns (x, true) if we should forward the message to the relayer, so it can be processed.
    /// Returns (x, false) if we should *not* forward the message to the relayer, because it will
    /// *not* be processed.
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L163-201)
```rust
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L308-324)
```rust
        let ack_resp = match ack_resp {
            Ok(ack) => ack,
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

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

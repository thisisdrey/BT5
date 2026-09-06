### Title
Broadcast-before-commit ordering in `process_stacker_db_chunks` allows StackerDB chunk propagation to diverge from persisted local state - ([File: stackslib/src/net/relay.rs])

### Summary
`PeerNetwork::process_stacker_db_chunks` writes newly-received StackerDB chunks into an open (uncommitted) SQL transaction, then immediately gossips each accepted chunk to the P2P network via `broadcast_message`, and only commits the SQL transaction after *all* chunks for that contract have been processed. If the final `tx.commit()` fails for any reason, every chunk written in that batch is rolled back by SQLite/rusqlite on transaction drop, but the corresponding `StackerDBPushChunk` gossip messages have already been sent to neighbors. This breaks the equality "chunk announced as stored/propagated == chunk actually committed to the local StackerDB", i.e. exactly the "no rollback of latest state updates" class of bug described in the report, applied to StackerDB chunk relaying instead of a smart-contract flow.

### Finding Description
In [1](#0-0) , for each contract's batch of `sync_results`:
1. `tx.try_replace_chunk(&sc, &md, &chunk.data)` writes the chunk into the slot inside an already-open `StackerDBTx` (a rusqlite transaction opened with `tx_begin_immediate`), per [2](#0-1)  and [3](#0-2) .
2. Immediately after a successful `try_replace_chunk`, the code calls `self.p2p.broadcast_message(vec![], msg)` to relay the chunk to neighbors — before the transaction is durably committed.
3. Only after the entire per-contract loop over `sync_results` finishes does the code call `tx.commit()?` at [4](#0-3) .

`StackerDBTx::commit` simply forwards to the underlying rusqlite `Transaction::commit` ( [5](#0-4) ); if it is never called (because the function returns early via `?` on failure, or the caller drops the `StackerDBTx`), rusqlite's default `Transaction::drop` behavior rolls back every write executed inside it — including all `insert_chunk` calls made earlier in the same loop, for potentially many already-broadcast chunks.

The consequence: the node can announce to its P2P neighbors (via `StackerDBPushChunk`) that it has newly stored specific slot/version chunks — which is the signal by which honest StackerDB neighbors decide to fetch/re-broadcast that chunk and update their StackerDB inventories — while its own on-disk replica silently reverts to the prior (older) version because the wrapping transaction failed to commit. This creates a durable mismatch between what was propagated as canonical/new to the network and what the node's own state actually holds, and it can repeat: a subsequent sync round can re-fetch/re-broadcast the same chunk since the node's own version counter never advanced, layering more spurious broadcasts.

A `tx.commit()` failure is not purely theoretical: `StackerDBs::tx_begin` uses `tx_begin_immediate`, which acquires a write lock immediately. Since the same on-disk StackerDB is also written concurrently through the unauthenticated `POST /v2/stackerdb/{principal}/{contract_name}/chunks` HTTP RPC endpoint ( [6](#0-5) ), an unauthenticated remote peer can increase write contention/lock pressure on the same SQLite database that `process_stacker_db_chunks` is committing, increasing the likelihood of a commit-time failure (e.g., `SQLITE_BUSY`) precisely at the point where already-broadcast chunks are pending commit.

### Impact Explanation
This matches the "network-wide propagation of forged data" / "steering a node off the tip via false inventory" impact classes: neighbors receive and process `StackerDBPushChunk` messages and update their local StackerDB replicas and inventories accordingly, while the origin node's own persisted slot version reverts. The result is an inconsistent view of "canonical" StackerDB state propagated across the network that does not correspond to what the originating node actually committed, which can cascade through subsequent StackerDB inventory/sync rounds (e.g. signer message relay via `.signers`/`.miners` StackerDB contracts).

### Likelihood Explanation
Triggering a mid-batch `tx.commit()` failure requires an underlying DB/IO error or write contention, which is not attacker-controlled with certainty, but write contention can be amplified remotely and without authentication by flooding the `POST /v2/stackerdb/.../chunks` RPC endpoint concurrently with ongoing StackerDB P2P sync activity, since both write paths share the same SQLite connection/lock. The ordering flaw itself (broadcast strictly before commit, across a multi-chunk batch) is deterministic and always present regardless of trigger likelihood.

### Recommendation
Reorder `process_stacker_db_chunks` so that `tx.commit()` happens before any `broadcast_message`/event-observer calls for the chunks written in that transaction (or commit per-chunk immediately after each successful `try_replace_chunk`, before broadcasting that chunk). This ensures a node never announces a chunk as newly stored to the network unless the corresponding write is already durably committed, preserving the equality between propagated state and persisted local state.

### Proof of Concept
1. Configure a node subscribed to a StackerDB contract and have a peer supply `StackerDBSyncResult`s containing multiple valid, properly-signed chunks for different slots in the same contract, processed by `process_stacker_db_chunks`.
2. Concurrently flood the node's unauthenticated `POST /v2/stackerdb/{principal}/{contract_name}/chunks` RPC endpoint with valid chunk-write requests targeting the same StackerDB contract, to increase the chance that the SQLite writer lock is contended when `process_stacker_db_chunks`'s final `tx.commit()` ( [4](#0-3) ) executes.
3. Observe that for chunks processed earlier in the same batch, `broadcast_message` ( [7](#0-6) ) already fired and neighbors update their inventories/replicas to the new slot version, while the node's own commit fails and its transaction (including those `insert_chunk` writes) is rolled back, leaving the node's local slot version at the old value — a persistent mismatch between announced and actual local StackerDB state.

### Citations

**File:** stackslib/src/net/relay.rs (L2406-2455)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
                            if matches!(e, Error::StaleChunk { .. }) {
                                // This is a common and expected message, so log it as a debug and with a sep message
                                // to distinguish it from other message types.
                                debug!(
                                    "Dropping stale StackerDB chunk";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            } else {
                                warn!(
                                    "Failed to store chunk for StackerDB";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            }
                            continue;
                        } else {
                            log_stored_stackerdb_chunk(&sync_result.contract_id, &chunk, &origin);
                        }

                        if let Some(event_list) = all_events.get_mut(&sync_result.contract_id) {
                            event_list.push(chunk.clone());
                        } else {
                            all_events.insert(sync_result.contract_id.clone(), vec![chunk.clone()]);
                        }

                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
                    }
                }
                tx.commit()?;
```

**File:** stackslib/src/net/stackerdb/db.rs (L187-190)
```rust
impl StackerDBTx<'_> {
    pub fn commit(self) -> Result<(), db_error> {
        self.sql_tx.commit().map_err(db_error::from)
    }
```

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

**File:** stackslib/src/net/stackerdb/mod.rs (L509-512)
```rust
    num_attempted_connections: u64,
    /// How many connections have been made in the last pass (gets reset)
    num_connections: u64,
    /// Number of state machine passes
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L181-197)
```rust
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
```

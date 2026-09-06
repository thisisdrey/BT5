### Title
Missing `write_freq` (wall-clock write-interval) enforcement in StackerDB chunk-acceptance path allows unauthenticated-rate abuse of the "one write per interval" guarantee - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The Cauldron `grab()` bug class is about a timing/interval threshold (`auctionInterval`) that is supposed to gate re-entry into a state transition but is either uninitialized or unenforced, letting callers repeatedly re-trigger the transition and starve forward progress. The direct analog in this repo is the StackerDB `write_freq` configuration value: it is faithfully parsed from the controlling smart contract in `StackerDBConfig::eval_config` [1](#0-0)  and stored on `StackerDBConfig`, and `SlotValidation` even tracks a `write_time` column intended to support such a check [2](#0-1) , but the actual chunk-write function, `StackerDBTx::try_replace_chunk`, never reads or enforces `write_freq`/`write_time` at all.

### Finding Description
`try_replace_chunk` is the single authoritative gate for accepting a new StackerDB chunk version into local storage. It checks: chunk size cap, existence of slot validation, signer/signature validity, staleness (`slot_version <= slot_validation.version`), and `max_writes`, then unconditionally calls `insert_chunk`: [3](#0-2) 

Nowhere in this function (nor in `insert_chunk`, which just updates the row and refreshes `write_time` to "now") is `self.config.write_freq` compared against the elapsed time since the slot's last `write_time`. `insert_chunk` unconditionally stamps `write_time` on every write [4](#0-3) , and the `write_freq` field is otherwise only used to fabricate `StackerDBConfig` from the smart contract; it is never consulted here.

This function is reached directly from two remote-facing paths, both accepting attacker/peer-controlled version bumps from any principal that legitimately owns a slot:
- The HTTP RPC handler `poststackerdbchunk.rs`, which stores the chunk and commits the transaction after only signer/version checks — no `write_freq` gate [5](#0-4) .
- The gossip-relay ingestion path `PeerNetwork::process_stacker_db_chunks`, which calls `tx.try_replace_chunk(&sc, &md, &chunk.data)` for every chunk obtained from peer sync results and, on success, immediately re-broadcasts it network-wide via `p2p.broadcast_message` [6](#0-5) .

By contrast, the doc comment on the DB test suite explicitly claims the write-frequency limit is enforced ("verifies that they cannot exceed the config-given wall-clock write frequency") [7](#0-6) , which does not match what `try_replace_chunk` actually does — mirroring the Cauldron report's core issue: an interval meant to gate re-entry that silently fails to be enforced at the state-mutation site.

### Impact Explanation
Any signer who legitimately owns a StackerDB slot (e.g., a signer set member writing signer messages, or any principal granted slots by a StackerDB-controlling contract) can submit new chunk versions as fast as `max_writes` permits, with zero wall-clock throttling, regardless of the `write-freq` value the controlling smart contract declares. Because every accepted write is immediately rebroadcast to the whole network via `broadcast_message` in `process_stacker_db_chunks`, this converts what should be a rate-limited channel (bounded by `write_freq`) into an unbounded one bounded only by `max_writes` and the per-request chunk-size cap. This can be used to flood the P2P StackerDB-chunk propagation channel across the network from a single authorized-but-malicious slot owner, consuming bandwidth/storage/CPU on every replica that syncs and relays the DB (a "High" class impact per the rules: bounded-compute/behavioral DoS reachable remotely without needing the node's own secret key, only a signer key that is expected to be untrusted-but-slot-scoped). This does not require the node's own secret key or an admin role — only the ordinary write authority already granted to any slot owner by the StackerDB contract, which is the threat model `write_freq` exists to constrain.

### Likelihood Explanation
Likelihood is high for any StackerDB instance whose controlling contract sets a nonzero `write-freq` expecting it to be enforced: the check is completely absent from the write path, so no non-trivial exploitation trick is needed — a legitimate slot owner (or a compromised/malicious signer key) need only issue rapid successive `POST /v2/stackerdb/.../chunks` (or push chunk gossip) with monotonically increasing `slot_version`, up to `max_writes`.

### Recommendation
1. In `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`), after loading `slot_validation`, compute elapsed time since `slot_validation.write_time` and reject (with a dedicated error, e.g. `TooFrequentWrites`) if it is less than `self.config.write_freq`, mirroring the existing `StaleChunk`/`TooManySlotWrites` checks.
2. Ensure both call sites (`poststackerdbchunk.rs` RPC handler and `relay.rs::process_stacker_db_chunks`) surface/log this new error the same way they do `StaleChunk`, so legitimate retries are distinguished from throttling.
3. Add/restore a test that actually exercises `write_freq > 0` end-to-end through `try_replace_chunk` (the current test comment claims this coverage exists but the code path shows no enforcement), to prevent regression.

### Proof of Concept
1. Configure (or use) a StackerDB contract with `write-freq` set to a large nonzero value (e.g., 3600 seconds) and `max-writes` set to a large number (e.g., 4096), as in the existing test fixture `TEST_CONTRACT` (`max-writes: u4096`) referenced in `stackslib/src/net/api/tests/poststackerdbchunk.rs` [8](#0-7) .
2. As the legitimate owner of slot 1 (`privk1`), sign and POST chunk version 1, then immediately sign and POST version 2, version 3, etc., back-to-back with no delay.
3. Observe via `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:398-438`) that every submission after the first succeeds immediately (bounded only by `max_writes`), with no rejection despite `write_freq` being configured to require, e.g., 3600 seconds between writes — because the function never reads `self.config.write_freq` or compares it to `slot_validation.write_time`.
4. Each accepted write, when received via the P2P sync/relay path, is rebroadcast to all peers by `PeerNetwork::process_stacker_db_chunks` (`stackslib/src/net/relay.rs:2445-2452`), demonstrating unthrottled network-wide propagation of an update stream that `write_freq` was designed to rate-limit.

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L422-437)
```rust
        let write_freq = config_tuple
            .get("write-freq")
            .expect("FATAL: missing 'write-freq'")
            .clone()
            .expect_u128()?;
        if write_freq > u64::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a write frequency beyond u64::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }
```

**File:** stackslib/src/net/stackerdb/db.rs (L86-90)
```rust
pub struct SlotValidation {
    pub signer: StacksAddress,
    pub version: u32,
    pub write_time: u64,
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-438)
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
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L240-299)
```rust
                    let slot_metadata_opt =
                        match tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id) {
                            Ok(slot_opt) => slot_opt,
                            Err(e) => {
                                // some other error
                                error!("Failed to load replaced StackerDB chunk metadata";
                                       "smart_contract_id" => contract_identifier.to_string(),
                                       "error" => format!("{:?}", &e)
                                );
                                return Err(StacksHttpResponse::new_error(
                                    &preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to load StackerDB chunk for {}: {:?}",
                                        &contract_identifier, &e
                                    )),
                                ));
                            }
                        };

                    let reason = serde_json::to_string(&err_code.clone().into_json())
                        .unwrap_or("(unable to encode JSON)".to_string());

                    let ack = StackerDBChunkAckData {
                        accepted: false,
                        reason: Some(reason),
                        metadata: slot_metadata_opt,
                        code: Some(err_code.code()),
                    };
                    return Ok(ack);
                }

                let slot_metadata = if let Ok(Some(md)) =
                    tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id)
                {
                    md
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(
                            "Failed to load slot metadata after storing chunk".to_string(),
                        ),
                    ));
                };

                if let Err(e) = tx.commit() {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(format!("Failed to commit StackerDB tx: {:?}", &e)),
                    ));
                }

                crate::net::stackerdb::log_stored_stackerdb_chunk(
                    &contract_identifier,
                    &stackerdb_chunk,
                    &crate::net::stackerdb::StackerDBChunkOrigin::Http { peer: http_peer },
                );

                // success!
                let ack = StackerDBChunkAckData {
                    accepted: true,
```

**File:** stackslib/src/net/relay.rs (L2406-2452)
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
```

**File:** stackslib/src/net/stackerdb/tests/db.rs (L325-330)
```rust
/// Test that we can insert and query chunks to a StackerDB.
/// * verifies that they must be signed
/// * verifies that they mut not be stale
/// * verifies that they cannot exceed the config-given wall-clock write frequency
/// * verifies that they cannot exceed the per-chunk write count
#[test]
```

**File:** stackslib/src/net/api/tests/poststackerdbchunk.rs (L298-313)
```rust
/// A chunk whose slot version exceeds the replica's configured `max_writes` must be reported
/// with the dedicated `TooManySlotWrites` error code.
#[test]
fn test_request_fail_too_many_slot_writes() {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 33333);

    let rpc_test = TestRPC::setup(function_name!());

    // The test StackerDB `TEST_CONTRACT` configures `max-writes: u4096`. Slot 1 is a fresh
    // slot (server version 0) owned by `privk1`, so a validly-signed, small chunk at a version
    // just past `max_writes` clears the size, slot, signature, and staleness checks and is
    // rejected specifically for exceeding the write budget.
    let data = "too many slot writes".as_bytes();
    let data_hash = Sha512Trunc256Sum::from_data(data);
    let mut slot_metadata = SlotMetadata::new_unsigned(1, 4097, data_hash);
    slot_metadata.sign(&rpc_test.privk1).unwrap();
```

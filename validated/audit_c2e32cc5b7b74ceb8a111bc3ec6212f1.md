## Title
StackerDB slot-ownership records are never cleared when a controlling contract revokes all signers, letting stale signers keep writing chunks - (File: `stackslib/src/net/stackerdb/mod.rs`)

## Summary
`StackerDBs::create_or_reconfigure_stackerdbs` reloads each StackerDB's authoritative signer set from its controlling smart contract every refresh cycle, but it deliberately skips the on-disk reconfiguration step whenever the freshly loaded config's signer list is empty. Because per-slot ownership (`chunks.signer`) is only ever updated inside `reconfigure_stackerdb`, a StackerDB whose contract-level authority has been revoked (or which the node failed to evaluate and silently fell back to a `noop()`/empty config) keeps its old, no-longer-authorized signer addresses bound to their slots forever. Any peer who is still recognized by the stale local record can continue to push signed chunks, and the node will accept, store, and re-gossip them as if they were still authoritative.

## Finding Description
The reconfigure gate in `create_or_reconfigure_stackerdbs` is: [1](#0-0) 

```rust
} else if (new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config
        && new_config.signers.len()
            != self.get_slot_versions(&stackerdb_contract_id)?.len())
{
    // only reconfigure if the config has changed
    if let Err(e) = self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config) {
```

If `new_config.signers.is_empty()`, the first disjunct is forced false regardless of whether the config actually changed, and (since `new_config != stackerdb_config` in this scenario) the second disjunct is also false. The whole branch is skipped, so `reconfigure_stackerdb` — the only function that rewrites the `signer` column of the `chunks` table (see `stackslib/src/net/stackerdb/db.rs`) — is never invoked. [2](#0-1) 

`reconfigure_stackerdb` is the only code path that resets a slot's stored `signer` when ownership changes; when it is skipped, whatever `StacksAddress` was previously recorded for each slot remains the value that `try_replace_chunk`/`get_slot_validation` treat as ground truth: [3](#0-2) 

Both the HTTP write path (`poststackerdbchunk.rs`) and the gossip/unsolicited-push validation path (`validate_received_chunk`) authorize a chunk purely by checking the *stored* `signer`/`slot_signer`, never by cross-checking it against the currently live contract-derived config: [4](#0-3) [5](#0-4) 

This is the exact class of bug the report describes: the entity meant to hold ultimate authority (the controlling contract, analogous to `StableSwapFactory`) updates its policy (revokes/empties the signer set, analogous to calling `pauseContract`), but the artifact that actually enforces the policy (the local StackerDB replica's stored slot ownership, analogous to `StableSwapThreePoolDeployer`) never receives the update because the code path that would apply it is gated off. The equality that should hold — "signer authorized to write to slot N" (on-chain, authoritative) equals "signer recorded as slot N's owner" (local DB, enforced) — is broken whenever the authoritative side degenerates to the empty set.

The empty-signers state is reachable through completely ordinary node operation, not just attacker-crafted input:
- `StackerDBConfig::from_smart_contract` genuinely returns whatever `stackerdb-get-signer-slots-page`/`stackerdb-get-signer-slots` evaluates to; a legitimate revocation to "no signers" produces an empty list.
- Any evaluation failure (RPC/Clarity error, contract not yet ready, DB error, `NoSuchStackerDB`, etc.) is silently mapped to `StackerDBConfig::noop()`, whose `signers` is empty, hitting the exact same skip condition: [6](#0-5) 

## Impact Explanation
Once the local replica silently fails to clear stale ownership, a remote party who was a legitimate signer under a prior configuration retains the ability to submit unauthenticated `POST /v2/stackerdb/{principal}/{contract}/chunks` requests (the endpoint has `security: []`) that the node will accept as validly signed, store, and then relay via `StackerDBPushChunk` gossip to the rest of the network: [7](#0-6) 

This is an unauthorized write to network state (a signer who should have been fully deauthorized keeps write/propagation rights) and results in network-wide propagation of data that should no longer be considered valid/authoritative, matching the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" criteria.

## Likelihood Explanation
No special privileges are needed beyond having once been a legitimate signer for the affected StackerDB — a realistic scenario since these DBs are used for the `.signers-*` and miner coordination contracts, where signer-set membership rotates every reward cycle. The condition is triggered by ordinary contract-state transitions to an empty signer set, or even transient evaluation failures that are silently coerced to an empty `noop()` config, making the stale-authorization window plausible during normal node operation rather than requiring a contrived attack.

## Recommendation
Remove the `!new_config.signers.is_empty()` special-case, or explicitly handle "signers became empty" as its own reconfiguration case that clears all slot ownership (mirroring what `reconfigure_stackerdb` already does for changed principals) rather than silently no-op'ing. Additionally, distinguish "contract legitimately returned empty signers" from "config evaluation failed" so that transient errors don't get treated the same as a real authoritative signer-set change, and so genuine revocations are always applied to the local replica's per-slot ownership records.

## Proof of Concept
1. Stand up a StackerDB replica with a controlling contract that currently authorizes signer `S` for slot 0; confirm `S` can `try_replace_chunk`/POST chunks successfully (per existing test coverage in `stackslib/src/net/stackerdb/tests/db.rs`).
2. Update the same contract so `stackerdb-get-signer-slots(-page)` now returns an empty list (full revocation) at the next chain tip, and trigger a config refresh (`create_or_reconfigure_stackerdbs`, called via `refresh_stackerdb` in `stacks-node/src/nakamoto_node/peer.rs`).
3. Observe that `new_config.signers.is_empty()` causes the reconfigure branch to be skipped; inspect the `chunks` table (or call `get_slot_validation`) and confirm slot 0's `signer` is still `S`.
4. From a remote client, submit `POST /v2/stackerdb/{principal}/{contract}/chunks` signed by `S` with a bumped version; observe the node accepts (`chunk_ack.accepted == true`) and issues a `StackerDBPushChunk` gossip message, even though the contract's current authoritative state says no one is authorized to write.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L376-405)
```rust
            } else {
                // attempt to load the config from the contract itself
                StackerDBConfig::from_smart_contract(
                    chainstate,
                    sortdb,
                    &stackerdb_contract_id,
                    num_neighbors,
                    connection_opts
                        .stackerdb_hint_replicas
                        .get(&stackerdb_contract_id)
                        .cloned(),
                )
                .unwrap_or_else(|e| {
                    if matches!(e, net_error::NoSuchStackerDB(_)) && stackerdb_contract_id.is_boot()
                    {
                        debug!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    } else {
                        warn!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    }
                    StackerDBConfig::noop()
                })
            };
```

**File:** stackslib/src/net/stackerdb/mod.rs (L414-428)
```rust
            } else if (new_config != stackerdb_config && !new_config.signers.is_empty())
                || (new_config == stackerdb_config
                    && new_config.signers.len()
                        != self.get_slot_versions(&stackerdb_contract_id)?.len())
            {
                // only reconfigure if the config has changed
                // (that second check on the length is needed in case the node is a victim of
                // #5142, which was a bug whereby a stackerdb could never shrink)
                if let Err(e) = self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config) {
                    warn!(
                        "Failed to create or reconfigure StackerDB {stackerdb_contract_id}: DB error {:?}",
                        &e
                    );
                }
            }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L302-327)
```rust
    pub fn reconfigure_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let mut total_slots_read = 0u32;
        for (principal, slot_count) in slots.iter() {
            total_slots_read =
                total_slots_read
                    .checked_add(*slot_count)
                    .ok_or(net_error::OverflowError(
                        "Slot count exceeeds u32::MAX".to_string(),
                    ))?;
            let slots_before_principal = total_slots_read - slot_count;
            for cur_principal_slot in 0..*slot_count {
                let slot_id = slots_before_principal + cur_principal_slot;
                if let Some(existing_validation) =
                    self.get_slot_validation(smart_contract, slot_id)?
                {
                    // this slot already exists.
                    if existing_validation.signer == *principal {
                        // no change
                        continue;
                    }
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-223)
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
                    // Classify the rejection directly from the error. `StaleChunk` is the
                    // only retryable case (the normal version-bump handshake); everything
                    // else is terminal for an identical chunk. Anything unexpected (DB or
                    // internal error) is a server error, not a client-classifiable ack, so
                    // it becomes an HTTP 500 rather than a misleading `accepted: false`.
                    let err_code = match &e {
                        NetError::StaleChunk { .. } => StackerDBErrorCodes::DataAlreadyExists,
                        NetError::NoSuchSlot(..) => StackerDBErrorCodes::NoSuchSlot,
                        NetError::BadSlotSigner(..) | NetError::VerifyingError(..) => {
                            StackerDBErrorCodes::BadSigner
                        }
                        NetError::StackerDBChunkTooBig(..) => StackerDBErrorCodes::ChunkTooBig,
                        NetError::TooManySlotWrites { .. } => {
                            StackerDBErrorCodes::TooManySlotWrites
                        }
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

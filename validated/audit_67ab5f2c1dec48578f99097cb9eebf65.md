## Finding: StackerDB Chunk Signatures Are Not Bound to a Specific StackerDB Contract, Enabling Cross-Contract Chunk Replay

### Title
Unauthenticated Cross-Contract StackerDB Chunk Replay via Missing Contract Binding in Slot Signature - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata`'s signed digest commits only to `slot_id`, `slot_version`, and `data_hash` — never to the StackerDB smart-contract identifier the chunk is destined for. [1](#0-0) 
Since the Nakamoto signer set assigns the *same* `(signer address, slot_id)` layout across every message-type StackerDB contract for a given signer set (`signers-{0|1}-0`, `signers-{0|1}-1`, ... `signers-{0|1}-(SIGNER_SLOTS_PER_USER-1)`), a signature that a signer legitimately produced for one contract is also a valid signature for the identical slot in a sibling contract.

### Finding Description
`SlotMetadata::sign`/`verify` compute the authenticated digest as: [2](#0-1) 
and verification recovers the public key from this same digest and compares against the expected `principal`: [3](#0-2) 

Both the write path (`try_replace_chunk`) and the p2p validation path (`validate_received_chunk`) authorize a chunk purely by resolving the expected signer address *for that one contract* and calling `slot_desc.verify(&signer)` — the contract identity itself never enters the signed material: [4](#0-3) [5](#0-4) 

Critically, the signer-slot assignment is *identical* across every per-message-type StackerDB contract for a signer set, as demonstrated directly by the codebase's own test, which iterates `message_id in 0..SIGNER_SLOTS_PER_USER` over contracts named `signers-{signer_set}-{message_id}` and asserts they all return the exact same `(address, slot)` ordering: [6](#0-5) 

Because chunks are public — they are gossiped over the p2p network and served by unauthenticated read RPCs, and posted via the unauthenticated `POST /v3/stackerdb/{contract}` endpoint which take the target contract straight from the URL path with no cross-check that it matches the chunk's originally-intended contract: [7](#0-6) 

...a remote, unprivileged attacker who observes any legitimately-signed chunk (e.g. a signer's `signers-0-0` BlockResponse chunk) can resubmit the exact same bytes (`slot_id`, `slot_version`, `sig`, `data`) to a sibling contract such as `signers-0-1` that assigns the same signer to the same `slot_id`. `try_replace_chunk` will accept it as long as the target contract's current version for that slot is below the replayed `slot_version` — which is essentially guaranteed the first time, since each contract's Lamport clock starts independently at 0.

Successful acceptance also triggers network-wide re-broadcast of the (mis-scoped) chunk to peers: [8](#0-7) 

This breaks the intended equality "signed-for-contract-A" == "valid-only-in-contract-A"; instead the signature authorizes the payload for *any* contract sharing that signer/slot mapping.

### Impact Explanation
This is an unauthenticated/unauthorized write into a StackerDB slot the attacker does not control the content of (the content is attacker-selected only in the sense of *which* previously-observed signed chunk to replay, and *where*), combined with automatic network-wide propagation of the (mismatched-context) chunk to all StackerDB-replicating peers. Consumers of a given StackerDB contract (e.g. signer-message pollers, `StackerDBListener`, `SignerDb::insert_state_machine_update` consumers) key their message-type interpretation on which contract the chunk arrived in; a chunk intended for one message type being force-fed into a different message-type contract can desynchronize downstream consumers (e.g. corrupting `GlobalStateEvaluator`/`SignerDb` state with data deserialized under the wrong `SignerMessageV0` variant expectations) and pollute a node's/network's view of that contract's slot state until a legitimately higher-versioned write overwrites it.

### Likelihood Explanation
No secret key material is required — only observation of any previously broadcast, validly-signed chunk (which is by design public/gossiped) and a single unauthenticated HTTP POST or p2p push to a sibling contract. The attack is trivially automatable and requires no privileged position on the network.

### Recommendation
Bind the signed digest to the specific StackerDB contract (and ideally reward cycle) by including the `QualifiedContractIdentifier` (or its hash) in `SlotMetadata::auth_digest`, and reject/ignore any chunk whose accompanying contract identity does not match what was signed. This closes the domain-separation gap between StackerDB contracts that intentionally share identical signer/slot layouts.

### Proof of Concept
1. Observe (via p2p gossip or `GET /v3/stackerdb/signers-0-0/{slot}`) a validly signed `StackerDBChunkData { slot_id, slot_version, sig, data }` submitted by signer `S` who occupies `slot_id = k` in `.signers-0-0`.
2. Because `.signers-0-1` (a sibling, different-message-type contract for the same signer set) assigns the identical `(S, k)` mapping (per `signers_db_get_slots` test behavior), issue `POST /v3/stackerdb/signers-0-1/chunks` with the exact same `slot_id`, `slot_version`, `sig`, and `data`.
3. `try_replace_chunk` for `.signers-0-1` looks up its own `slot_validation.signer` for slot `k` (which is `S`), calls `slot_desc.verify(&S)` — which succeeds because the digest never referenced the contract — and, since `.signers-0-1`'s local version for slot `k` is lower, accepts and stores the chunk, then broadcasts it network-wide via `process_stacker_db_chunks`.

**Uncertainty / limitations noted:** I could not directly view the `MessageSlotID::stacker_db_contract` implementation or `SIGNER_SLOTS_PER_USER`'s numeric value inside this index (only reference counts were retrievable, not full file contents), so the exact set of message-type contracts sharing a signer set is inferred from the `signers_db_get_slots` test's assertions rather than from reading `libsigner/src/v0/messages.rs` directly. The core vulnerability — `auth_digest` omitting contract binding — is directly confirmed in source.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-166)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
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

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-341)
```rust
    for signer_set in 0..2 {
        for message_id in 0..SIGNER_SLOTS_PER_USER {
            let contract_name =
                ContractName::try_from(format!("signers-{}-{}", &signer_set, &message_id)).unwrap();
            let signers = readonly_call(
                &mut peer,
                &latest_block_id,
                contract_name.clone(),
                ClarityName::from_literal("stackerdb-get-signer-slots"),
                vec![],
            )
            .expect_result_ok()
            .unwrap();

            debug!("Check .{}", contract_name);
            if signer_set == 0 {
                assert_eq!(signers.expect_list().unwrap(), vec![]);
            } else {
                assert_eq!(signers, expected_stackerdb_slots);
            }
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

**File:** stackslib/src/net/relay.rs (L2410-2452)
```rust
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

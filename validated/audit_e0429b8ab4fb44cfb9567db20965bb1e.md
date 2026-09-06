## Analysis

The reported PoolTogether bug is a **missing binding/domain-separation** issue: a value that should be scoped to one authorization context (a specific promotion/ticket) is accepted globally because the verification check didn't tie the claim to the right context. The strongest analog in this repo is in the **StackerDB chunk-signing scheme**, where the signature that authenticates a chunk write does not bind to the destination StackerDB (`QualifiedContractIdentifier`), only to `(slot_id, slot_version, data_hash)`. [1](#0-0) 

This digest is used everywhere a chunk is accepted for storage/replication: `StackerDBTx::try_replace_chunk` (validates size/staleness/signer but calls `slot_desc.verify(&slot_validation.signer)` — no contract binding), and `PeerNetwork::validate_received_chunk` (same: fetches signer for `(smart_contract_id, slot_id)` then calls `slot_metadata.verify(&addr)` — again no contract binding). [2](#0-1) [3](#0-2) 

Critically, the Nakamoto signer protocol creates **multiple sibling StackerDB contracts** (`signers-{set}-{message_id}`) that all derive their slot ↔ signer-address assignment from the exact same source (`stackerdb-get-signer-slots-page`), so the *same signer address occupies the identical slot_id in every sibling lane contract* for a given signer set/reward cycle. [4](#0-3) [5](#0-4) 

Because the signature never commits to which contract it's for, a validly-signed chunk observed on one lane/cycle (these are broadcast in the clear over p2p/HTTP) can be re-submitted verbatim to a *different* sibling StackerDB contract where that address occupies the same slot, and it will pass all network-layer checks and be stored and rebroadcast network-wide, exactly like a freshly-authorized write. [6](#0-5) 

The only place any payload-type/lane binding is checked is an **application-layer, non-network-layer filter** used solely by the signer daemon when converting `StackerDBChunksEvent` into typed messages — it does not run in the node's storage/replication path and explicitly only covers currently-known v0 message-type prefixes: [7](#0-6) 

That means a stale `BlockPreCommit`/`BlockResponse`/`StateMachineUpdate` chunk from a prior reward cycle's `signers-{set}-{message_id}` contract can be replayed into the *current* cycle's identically-indexed sibling contract (same message_id, same slot order) — same message-type prefix, so it even passes the app-layer lane filter — purely because `SlotMetadata::auth_digest()` never binds to the destination contract.

### Title
Missing contract-id binding in StackerDB chunk signatures allows cross-contract/cross-cycle signature replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` (and thus every chunk signature) commits only to `(slot_id, slot_version, data_hash)` — never to the target `QualifiedContractIdentifier`. Because sibling Nakamoto `.signers-{set}-{message_id}` StackerDB contracts assign the same signer address to the same `slot_id`, a legitimately-signed, publicly-broadcast chunk from one contract (e.g. an old reward-cycle's `signers-0-1`) can be resubmitted unmodified to a different sibling contract (e.g. current cycle's `signers-0-1`, or a different message-id lane) and pass all node-side verification, causing it to be stored and rebroadcast network-wide as if freshly authorized.

### Finding Description
- `SlotMetadata::verify()` and `StackerDBChunkData::verify()` only check that the signature recovers to the expected signer's public-key hash over `auth_digest = H(slot_id || slot_version || data_hash)`. [1](#0-0) 
- Both write paths — HTTP upload (`poststackerdbchunk.rs` → `StackerDBTx::try_replace_chunk`) and p2p push/sync (`PeerNetwork::validate_received_chunk` / `handle_unsolicited_StackerDBPushChunk`) — resolve the expected signer strictly from `(smart_contract, slot_id)` and then call the *same* contract-agnostic `verify()`. [8](#0-7) [9](#0-8) 
- Sibling `.signers-{set}-{message_id}` contracts (message ids: `BlockResponse=1`, `StateMachineUpdate=2`, `BlockPreCommit=3`) all read the identical slot→signer ordering from `.signers`'s `stackerdb-get-signer-slots-page`, so a given signer occupies the same `slot_id` across every lane and reward-cycle-parity contract. [10](#0-9) [5](#0-4) 
- A chunk successfully stored is unconditionally rebroadcast to the whole network via `PeerNetwork::broadcast_message`. [11](#0-10) 
- The only defense that inspects the payload's message-type against its expected lane is an application-layer helper (`signer_message_payload_matches_lane`) used exclusively when converting node-emitted `StackerDBChunksEvent`s into typed `SignerEvent`s for the signer daemon — it never runs inside the node's storage/replication path, and the code comment states it is "fixed to v0 `SignerMessageTypePrefix` semantics" and must be manually extended for future message versions. [7](#0-6) 

Consequently, the equality "signed for destination contract C" is never checked; only "signed by the address currently on record for `slot_id` in whichever contract asked" is checked. Replaying a same-lane, same-message-type, older-reward-cycle chunk (e.g. a stale `BlockPreCommit`) into the current cycle's identically-indexed sibling contract bypasses even the application-layer type filter, since the message-type prefix matches exactly.

### Impact Explanation
This breaks the "authenticated-for-X vs stored-as-Y" equality described in the rules: forged-context data is accepted and propagated network-wide by every node replicating that StackerDB, without needing any private key the attacker doesn't already legitimately possess as an eavesdropper of public broadcast traffic. It can be used to inject stale/incorrect `BlockPreCommit`/`BlockResponse`/`StateMachineUpdate` data into a live reward cycle's signer coordination channel, which downstream tooling that doesn't apply (or can't apply, for newer message versions) the app-layer lane filter will treat as canonical, current-cycle signer state — matching "network-wide propagation of forged data" (Critical).

### Likelihood Explanation
High. No signing capability is required by the attacker beyond capturing chunks that are already broadcast in cleartext over the p2p network (or fetched via the StackerDB HTTP endpoints). The attacker only needs to resubmit the identical bytes to a different contract endpoint via `POST /v2/stackerdb/.../chunks` or via p2p `StackerDBPushChunk`, exploiting the deterministic, contract-independent slot assignment that is a designed and tested property of the `.signers-*` contract family.

### Recommendation
Include the destination `QualifiedContractIdentifier` (and ideally the reward-cycle/message-id) in `SlotMetadata::auth_digest()`, so a signature is only valid for the specific StackerDB it was created for. This requires a versioned chunk/metadata format change and coordinated rollout, since it affects the wire-level `StackerDBChunkData` semantics consumed by `libstackerdb`, `stackslib/src/net/stackerdb`, and `libsigner`.

### Proof of Concept
1. Wait for/observe (via p2p sniffing or the public StackerDB chunk-download RPC) a validly-signed `BlockPreCommit` chunk `C` written by signer `S` at `slot_id=k`, `slot_version=v` in `signers-0-3` for reward cycle `N`.
2. When reward cycle `N+2` begins (same parity, `reward_cycle % 2 == 0`) and `S` is re-selected with the same sort position (common given signer-set stability), its `signers-0-3` contract slot `k` starts fresh at low version.
3. Submit chunk `C` unmodified via `POST /v2/stackerdb/<contract-id-for-cycle-N+2>/chunks` (or via `StackerDBPushChunk`) — `try_replace_chunk`/`validate_received_chunk` accept it because `slot_desc.verify(&signer)` only checks `(slot_id, slot_version, data_hash)`, all of which match, and the destination signer address at that slot is identical.
4. The node stores and rebroadcasts `C` to the whole network as a legitimate, fresh `BlockPreCommit` for cycle `N+2`.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-193)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }

    /// Sign this slot metadata, committing to slot_id, slot_version, and
    /// data_hash.  Sets self.signature to the signature.
    /// Fails if the underlying crypto library fails
    pub fn sign(&mut self, privkey: &StacksPrivateKey) -> Result<(), Error> {
        let auth_digest = self.auth_digest();
        let sig = privkey
            .sign(&auth_digest.0)
            .map_err(|se| Error::SigningError(se.to_string()))?;

        self.signature = sig;
        Ok(())
    }

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
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
    }
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1058-1073)
```rust
    }

    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
    }
```

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/net/relay.rs (L2406-2453)
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
```

**File:** libsigner/src/events.rs (L568-596)
```rust
        } else if event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot() {
            let Some((signer_set, message_id)) =
                get_signers_db_signer_set_message_id(event.contract_id.name.as_str())
            else {
                return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
            };
            // signer-XXX-YYY boot contract
            //
            // NOTE: the payload-type check below uses v0 `SignerMessageTypePrefix` semantics
            // (the mapping in `signer_message_payload_matches_lane` is fixed to v0). Future
            // signer-message versions must extend that mapping, or their chunks will not be
            // recognized here regardless of which `T` is in scope.
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
```

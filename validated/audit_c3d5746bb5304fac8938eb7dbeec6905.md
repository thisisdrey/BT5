### Title
StackerDB chunk signature omits the smart-contract identifier, enabling cross-replica signature replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
The signed digest that authenticates a StackerDB chunk (`SlotMetadata::auth_digest`) only commits to `slot_id`, `slot_version`, and `data_hash`. It never binds the signature to the specific StackerDB smart contract (`QualifiedContractIdentifier`) the chunk belongs to. Because signer sets and slot indices are frequently identical or overlapping across different `.signers-*` StackerDB replicas (e.g. consecutive reward-cycle contracts reusing the same signer ordering), a chunk that was validly signed and broadcast for one replica can be replayed by any observer into a different replica where the same `StacksAddress` owns the same `slot_id`, exactly analogous to the reported Uniswap issue where a value (there, `poolFee`; here, the signing digest) is not scoped to the specific "pair"/context it is meant to authorize.

### Finding Description
`SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs` (lines 159-166) hashes only:
```
hasher.update(self.slot_id.to_be_bytes());
hasher.update(self.slot_version.to_be_bytes());
hasher.update(self.data_hash.0);
```
No `smart_contract_id` / `stackerdb_id` is included in the digest that `SlotMetadata::sign`/`SlotMetadata::verify` operate on (lines 168-193) or in `StackerDBChunkData::sign`/`verify` (lines 223-244), which simply delegate to the same digest.

Verification of received chunks is scoped by contract only at the *lookup* stage, not in the cryptographic commitment itself:
- `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-437`) looks up `slot_validation.signer` for the given `smart_contract` + `slot_id`, then calls `slot_desc.verify(&slot_validation.signer)`. The verification only checks that the signature recovers to that address — it cannot detect that the signature was actually produced (and broadcast) for a *different* contract's identical slot.
- `PeerNetwork::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-718`) has the identical pattern: it fetches the expected signer via `self.stackerdbs.get_slot_signer(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)?`, again with no contract-scoped domain separation in the digest itself.

Because many StackerDB replicas configured for signer communication reuse identical signer sets and identical slot ordering across sequential reward cycles (this is a deliberate design choice for stability of signer slot indices), an attacker who merely observes a previously-broadcast, validly-signed `StackerDBChunkData` for replica A can resubmit the exact same `(slot_id, slot_version, sig, data)` tuple as a `StackerDBPushChunkData` targeting replica B. If the same `StacksAddress` is registered as the signer of that `slot_id` in replica B (a common real-world configuration), `try_replace_chunk`/`validate_received_chunk` will accept and store it as if the signer had freshly authorized that exact chunk for replica B, even though the signer's signature never mentioned that contract at all.

This breaks the intended equality: "signature valid for slot X in contract A" should not imply "authorized for slot X in contract B" — but the digest construction makes these two statements cryptographically indistinguishable.

### Impact Explanation
This allows an unprivileged remote peer (anyone who observed the original gossip message, which is by design publicly broadcast/relayed) to write attacker-selected, previously-signed data into a StackerDB slot in a different replica context without that signer ever intending or approving it for that context. This is an unauthorized/forged write into shared network state (StackerDB), and given StackerDB is the transport signers use to coordinate block signing, cross-replica replay of stale signer messages (e.g. resurrecting an old reward cycle's signature/message as if fresh for the current cycle) could mislead consumers of that StackerDB content. This matches the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact category.

### Likelihood Explanation
Likelihood is Medium-to-High in practice: it requires (1) the same `StacksAddress` to be the registered signer for the same `slot_id` across two different StackerDB contracts (plausible/common given how signer sets and slot assignments are constructed across consecutive reward cycles), and (2) a previously broadcast, validly-signed chunk to exist for replay (trivially obtainable, since StackerDB chunks are relayed over the p2p network in plaintext). No secret key or privileged role is required by the attacker — only replaying data they already observed.

### Recommendation
Bind the signature to the specific StackerDB replica by including the `smart_contract_id` (or an equivalent replica-unique domain separator, e.g. `stackerdb_id`) in `SlotMetadata::auth_digest()`, and thread that value through `sign`/`verify` on both the signer and validator sides (`libstackerdb/src/libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs::try_replace_chunk`, `stackslib/src/net/stackerdb/mod.rs::validate_received_chunk`). This mirrors the report's recommendation to make `poolFee` (context-defining parameter) an explicit, checked input rather than an implicit global default.

### Proof of Concept
1. Signer key `K` is registered as the owner of `slot_id = 0` in both StackerDB contract `A` (`.signers-1-N`) and contract `B` (`.signers-2-N`), which is the typical layout since slot ordering is preserved across consecutive reward cycles.
2. Signer legitimately signs and broadcasts a chunk for contract `A`: `StackerDBChunkData { slot_id: 0, slot_version: 5, sig: S, data: D }`, where `S = sign(auth_digest(slot_id=0, slot_version=5, hash(D)))`. This is relayed p2p-wide via `StackerDBPushChunk` (`stackslib/src/net/relay.rs:2445-2452`).
3. Attacker (any peer) observes this message on the wire, and re-wraps it as `StackerDBPushChunkData { contract_id: B, chunk_data: StackerDBChunkData { slot_id: 0, slot_version: 5, sig: S, data: D } }`, and sends it to a node that also replicates contract `B`.
4. `PeerNetwork::handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-718`) fetches `addr = get_slot_signer(B, 0)`, which equals `K`'s address, and `slot_metadata.verify(&addr)` succeeds because the digest never referenced contract `A` or `B`.
5. The chunk is accepted and stored into contract `B`'s slot 0 (via `try_replace_chunk`, `stackslib/src/net/stackerdb/db.rs:400-437`) and re-broadcast, even though the signer never signed anything for contract `B`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** libstackerdb/src/libstackerdb.rs (L223-244)
```rust
    /// Sign this given chunk data message with the given private key.
    /// Sets self.signature to the signature.
    /// Fails if the underlying signing library fails.
    pub fn sign(&mut self, privk: &StacksPrivateKey) -> Result<(), Error> {
        let mut md = self.get_slot_metadata();
        md.sign(privk)?;
        self.sig = md.signature;
        Ok(())
    }

    pub fn recover_pk(&self) -> Result<StacksPublicKey, Error> {
        let digest = self.get_slot_metadata().auth_digest();
        StacksPublicKey::recover_to_pubkey_without_validating_low_s(digest.as_bytes(), &self.sig)
            .map_err(|ve| Error::VerifyingError(ve.to_string()))
    }

    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
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

### Title
StackerDB chunk signatures do not bind the target contract/message-type, enabling cross-contract signature replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
A `StackerDBChunkData`/`SlotMetadata` signature only commits to `(slot_id, slot_version, data_hash)`. It never binds the StackerDB `contract_id` (or message type) that the chunk is destined for. Because slot assignment for the `.signers` StackerDB replicas is computed purely from the sorted list of signer public keys and is identical across every message-type contract within a given signer set (`signers-<set>-<message_id>`), the *same* signer occupies the *same* `slot_id` in many different contracts. A validly-signed chunk captured from one contract can therefore be replayed, unmodified, into a different contract where that signer owns the same slot, and it will pass all signature checks.

### Finding Description
`SlotMetadata::auth_digest()` in [1](#0-0)  hashes only `slot_id`, `slot_version`, and `data_hash` — no contract identifier, network ID, or message-type discriminator is included in the signed digest. `SlotMetadata::verify()` recovers the pubkey from exactly this digest and checks it against the expected address [2](#0-1) .

Storage-side, `StackerDBTx::try_replace_chunk` looks up the expected signer for `(smart_contract, slot_id)` and calls `slot_desc.verify(&slot_validation.signer)`, then only checks staleness/`max_writes` — it never checks that the signature was produced *for this specific contract* [3](#0-2) . The same pattern exists for chunks received over the p2p network in `validate_received_chunk`, which again verifies only `slot_metadata.verify(&addr)` plus version/size checks, with no contract binding in the signed payload [4](#0-3) .

Critically, the `.signers` boot contract assigns slots identically across every message-type sub-contract for a signer set: `signers_db_get_slots` shows the exact same `expected_stackerdb_slots` list (i.e., same signer → same slot index) returned for every `message_id` in `0..SIGNER_SLOTS_PER_USER` for a given signer set [5](#0-4) , confirmed by `stackerdb-get-signer-slots-page` in the Clarity contract, which serves one shared per-cycle slot list to all consuming contracts [6](#0-5) .

The write path is reachable by any unprivileged remote party via the public HTTP endpoint `POST /v2/stackerdb/<contract>/chunks`, whose ack error codes (`BadSigner`, `DataAlreadyExists`, etc.) are defined in [7](#0-6) , and by unsolicited `StackerDBPushChunk` p2p gossip messages handled in `handle_unsolicited_StackerDBPushChunk` [8](#0-7) , neither of which adds any contract-binding check beyond what `verify()`/`try_replace_chunk` already (insufficiently) perform.

### Impact Explanation
An attacker who observes any validly-signed chunk broadcast for signer S at `slot_id = K` in contract A (e.g., the `BlockResponse` contract for a signer set) can repost the exact same `StackerDBChunkData` bytes to contract B (e.g., a different message-type contract, or the same message-type contract in a different reward cycle where the same signer maps to the same slot after a set rotation), as long as B's current slot version for K is lower than the replayed chunk's version. The chunk passes `BadSigner`/signature checks (since verification never examines which contract it targets) and is accepted, stored, and then rebroadcast to the whole network via `process_stacker_db_chunks`/`broadcast_message` [9](#0-8) . This is an unauthenticated/unauthorized write of forged data (data never actually produced for that context by the signer) that gets served and propagated as canonical signer state — matching the report's "signature/value not scoped to its specific context, so it can be replayed to produce results the signer never authorized."

### Likelihood Explanation
No special privilege is required: StackerDB chunk contents and signatures are broadcast in the clear over p2p (`StackerDBPushChunk`) and are also servable/verifiable by any node via public reads; the write RPC and p2p push-acceptance path are open to any peer. The only preconditions are (a) the destination contract/slot has a lower stored version than the captured chunk (commonly true right after a signer-set/contract rotation, where new StackerDB instances start at version 0), and (b) the same signer key maps to the same `slot_id` in both contracts, which is guaranteed by design for all message-type sub-contracts of a given signer set.

### Recommendation
Include a domain separator that binds the signature to the specific StackerDB instance in `SlotMetadata::auth_digest()` — e.g., hash the `contract_id` (and/or `rc_consensus_hash`/network id) together with `slot_id`, `slot_version`, and `data_hash` — so a signature produced for one StackerDB contract cannot be replayed into another, mirroring the report's fix of binding the signature to the specific context it authorizes.

### Proof of Concept
1. Identify two StackerDB contracts for the same signer set, e.g. `signers-1-<msgid_A>` and `signers-1-<msgid_B>`, where signer S is assigned `slot_id = K` in both (guaranteed per `signers_db_get_slots`).
2. Observe (via p2p gossip or a normal StackerDB read) a validly-signed `StackerDBChunkData{slot_id: K, slot_version: V, sig, data}` that S posted to contract A.
3. Submit the identical `StackerDBChunkData` bytes via `POST /v2/stackerdb/<contract_B>/chunks` (or replay it as an unsolicited `StackerDBPushChunk` p2p message) targeting contract B, where B's current version for slot K is `< V`.
4. `try_replace_chunk`/`validate_received_chunk` verify only `(slot_id, slot_version, data_hash)` against S's address — this succeeds — and the chunk is stored under contract B and rebroadcast, even though S never signed anything for contract B.

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

**File:** libstackerdb/src/libstackerdb.rs (L181-193)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L411-437)
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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-716)
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

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

```

**File:** stackslib/src/net/stackerdb/mod.rs (L742-766)
```rust
    pub fn handle_unsolicited_StackerDBPushChunk(
        &mut self,
        chainstate: &mut StacksChainState,
        event_id: usize,
        preamble: &Preamble,
        chunk_data: &StackerDBPushChunkData,
        send_reply: bool,
    ) -> Result<(bool, bool), net_error> {
        let Some(naddr) = self
            .get_p2p_convo(event_id)
            .map(|convo| convo.to_neighbor_address())
        else {
            debug!(
                "Drop unsolicited StackerDBPushChunk: event ID {} is not connected",
                event_id
            );
            return Ok((false, false));
        };

        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L96-132)
```rust
#[derive(Debug, Clone, PartialEq)]
pub enum StackerDBErrorCodes {
    /// The slot already holds a chunk whose version is at least the one submitted.
    DataAlreadyExists,
    /// The chunk's slot ID is out of range for this replica's slot allocation.
    NoSuchSlot,
    /// The chunk's signature does not recover to the address that owns the slot.
    BadSigner,
    /// The chunk exceeds the replica's configured chunk size.
    ChunkTooBig,
    /// The chunk's slot version exceeds the replica's configured maximum writes.
    TooManySlotWrites,
}

impl StackerDBErrorCodes {
    pub fn code(&self) -> u32 {
        match self {
            Self::DataAlreadyExists => 0,
            Self::NoSuchSlot => 1,
            Self::BadSigner => 2,
            Self::ChunkTooBig => 3,
            Self::TooManySlotWrites => 4,
        }
    }

    #[cfg_attr(test, mutants::skip)]
    pub fn reason(&self) -> &'static str {
        match self {
            Self::DataAlreadyExists => "Data for this slot and version already exist",
            Self::NoSuchSlot => "No such StackerDB slot",
            Self::BadSigner => "Signature does not match slot signer",
            Self::ChunkTooBig => "Chunk exceeds the replica's configured chunk size",
            Self::TooManySlotWrites => {
                "Slot version exceeds the replica's configured maximum writes"
            }
        }
    }
```

**File:** stackslib/src/net/relay.rs (L2445-2452)
```rust
                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
```

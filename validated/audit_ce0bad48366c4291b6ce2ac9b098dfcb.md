### Title
Chunk signature omits any contract/domain binding or expiry, enabling cross-StackerDB replay of authenticated chunks - (File: `libstackerdb/src/libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs`, `stackslib/src/net/api/poststackerdbchunk.rs`)

### Summary
### Finding Description
`SlotMetadata::auth_digest` computes the digest that a signer authenticates over `slot_id`, `slot_version`, and `data_hash` only, with no binding to the target StackerDB contract, chain view, or any expiry/deadline field: [1](#0-0) 
`sign`/`verify` operate purely on this digest: [2](#0-1) .

Because the signature never expires and does not commit to *which* StackerDB (`QualifiedContractIdentifier`) it was intended for, the exact same signed `StackerDBChunkData` (slot_id, slot_version, data, sig) remains "valid" forever, and is valid against **any** StackerDB replica in which that signer happens to own the same `slot_id` — exactly the "forever-valid, out-of-context request" bug class from the analog report (missing deadline/domain separation in a signed authorization).

On the write path, `StackerDBTx::try_replace_chunk` only checks: chunk size, `slot_desc.verify(&slot_validation.signer)` (which recovers the signer from the domain-less digest), monotonic version, and max_writes — it never checks that the signature was produced for *this* `smart_contract` (the contract id is passed in only to look up local slot ownership, not fed into the digest): [3](#0-2) 

The same domain-less scheme is used on both the HTTP write endpoint and the gossiped push path. The HTTP handler `RPCPostStackerDBChunkRequestHandler::try_handle_request` takes the `contract_identifier` purely from the URL path and hands the attacker-supplied `stackerdb_chunk` straight to `try_replace_chunk` for that contract — the chunk's signature says nothing about which contract it was meant for: [4](#0-3) 
And on acceptance, the node re-broadcasts it network-wide as a `StackerDBPushChunk` tagging it with *this* `contract_id`: [5](#0-4) 

The gossip-side validator `PeerNetwork::validate_received_chunk` reaches the identical conclusion: it resolves the slot's expected signer from `smart_contract_id` (the contract named in the *envelope*, not committed by the signature) and then simply calls `slot_metadata.verify(&addr)`, so any chunk whose signature recovers to that address is accepted for that DB regardless of which DB it was originally signed/broadcast for: [6](#0-5) 

Also relevant: `StackerDBTx::reconfigure_stackerdb` preserves the existing `version` counter (does not reset it) whenever a slot's signer principal is unchanged across a reconfiguration — it only resets `version` to `NO_VERSION` when the signer address for that slot_id changes: [7](#0-6) 
This means the only thing preventing a captured signed chunk from being replayed back into the *same* contract is the monotonic version counter, but the counter is a purely local, per-DB integer independent of the signature — it provides no protection across different StackerDB contracts, since each contract tracks its own independent counter starting from whatever state it happens to be in.

### Impact Explanation
Any unprivileged network participant who observes a `StackerDBPushChunk` (or fetches a chunk via `StackerDBGetChunk`) for one contract can resubmit the identical, still cryptographically-valid `StackerDBChunkData` (same `slot_id`, `slot_version`, `sig`, `data`) to a *different* StackerDB replica hosted by any node, addressed at a different `contract_id`, as long as:
1. that other contract's slot table assigns the same `slot_id` to the same signer address, and
2. that other contract's current stored version for that slot is `< slot_version` of the captured chunk (trivially true for a freshly-initialized or lower-activity replica).

`try_replace_chunk`/`validate_received_chunk` will accept it as an authentic, freshly-signed chunk for the unrelated contract, and the receiving node will re-gossip it network-wide as `StackerDBPushChunk` for that contract — i.e., forged/out-of-context data is propagated and stored as legitimate for a StackerDB instance the original signer never intended to write. Because StackerDB is used to carry mempool/signer/miner coordination messages that downstream code (e.g. `stacks-signer`) trusts as authentic for their specific contract, this is an unauthenticated write into a different logical namespace than the signer authorized, meeting the "Critical: unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data" bar.

### Likelihood Explanation
Requires no privileged access, no secret key, and no more than passively observing one legitimately-broadcast chunk on the public gossip network (StackerDB chunks are, by design, broadcast/replicated data) and then resubmitting it via the standard, unauthenticated `POST /v2/stackerdb/:address/:contract/chunks` RPC endpoint or the P2P push path against a different target contract. The main precondition — the same signer owning the same `slot_id` across two different StackerDB contracts — is plausible in this codebase because slot assignments are derived deterministically from ordered signer/miner lists (e.g., successive reward-cycle signer-set contracts), making identical `(signer, slot_id)` pairs across sibling contracts a realistic occurrence rather than a contrived edge case.

### Recommendation
Include a domain-separation tag in `SlotMetadata::auth_digest` — at minimum the `QualifiedContractIdentifier` of the target StackerDB (and ideally the network id / chain view or an explicit expiry), so a chunk signature is cryptographically bound to exactly one contract and cannot be replayed elsewhere. This mirrors the analog recommendation of adding a deadline/domain binding to signed payloads so they cannot be replayed indefinitely or out of context. Any digest format change is a wire/protocol breaking change and needs coordinated rollout.

### Proof of Concept
1. Set up two StackerDB contracts, `A` and `B`, both configuring the same signer address to own `slot_id = 0` (achievable in this codebase since slot ownership is derived from ordered signer lists, as shown by `create_stackerdb`/`reconfigure_stackerdb`).
2. Signer legitimately signs and pushes `StackerDBChunkData{slot_id:0, slot_version:5, data, sig}` to contract `A` via the normal path exercised in `libstackerdb/src/tests/mod.rs` (`sign`/`verify`) and `stackslib/src/net/stackerdb/tests/db.rs::test_stackerdb_insert_query_chunks`.
3. Attacker observes this chunk (via gossip `StackerDBChunkInv`/`StackerDBGetChunk`, or the HTTP response) and resubmits the identical `sig`/`slot_id`/`slot_version`/`data` to contract `B`'s `POST /v2/stackerdb/:address/:contract/chunks` endpoint (`stackslib/src/net/api/poststackerdbchunk.rs`), where contract `B`'s stored version for slot 0 is `< 5`.
4. `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400`) recovers the signer via `slot_desc.verify(&slot_validation.signer)`, which succeeds because the digest never referenced contract `A`; version check passes since 5 > B's stored version; chunk is stored under `B` and re-gossiped as authentic for `B`.

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

**File:** libstackerdb/src/libstackerdb.rs (L171-193)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L302-347)
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

                debug!("Reset slot {} of {}", slot_id, smart_contract);

                // new slot, or existing slot with a different signer
                let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
                let mut stmt = self.sql_tx.prepare(qry)?;
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];

                stmt.execute(args)?;
            }
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L169-201)
```rust
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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-706)
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
```

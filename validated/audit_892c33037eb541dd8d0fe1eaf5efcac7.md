### Title
StackerDB chunk signatures omit the smart-contract identifier, enabling cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The signature that authenticates a StackerDB chunk write (`SlotMetadata::auth_digest`) commits only to `slot_id`, `slot_version`, and `data_hash` — it never binds the signature to the specific StackerDB smart contract (replica) the chunk is destined for. Every place that verifies a chunk's authenticity (`StackerDBs::try_replace_chunk`, `StackerDBSync::validate_received_chunk`) checks the recovered signer against the *slot's* configured signer for the *target* contract, but never checks that the signature was produced for *that* contract. Consequently, a chunk that was validly signed and broadcast for one StackerDB instance (e.g., one PoX reward cycle's `.signers-X-Y` contract) can be replayed by any relaying peer into a different StackerDB instance that happens to assign the same signer to the same `slot_id`, causing stale/out-of-context signed data to be accepted as valid, current data for a different contract/epoch.

### Finding Description
The digest that gets signed is computed in `SlotMetadata::auth_digest`: [1](#0-0) 

Note that it hashes only `slot_id`, `slot_version`, and `data_hash`. There is no domain separator tying the signature to a specific `QualifiedContractIdentifier` (the StackerDB replica/contract).

`SlotMetadata::verify` and `StackerDBChunkData::verify` simply recover the public key from this digest and compare its hash to the expected principal: [2](#0-1) 

Both server-side acceptance paths rely solely on this signature check plus a per-contract slot/version lookup, but never verify that the signature was produced with the target contract in mind:

- The database-level write path, `StackerDBTx::try_replace_chunk`, looks up the expected signer for `(smart_contract, slot_id)` and calls `slot_desc.verify(&slot_validation.signer)`, which only checks `(slot_id, slot_version, data_hash)` against the signer — not the contract itself: [3](#0-2) 

- The P2P/gossip validation path, `StackerDBSync::validate_received_chunk`, does the same: it fetches `get_slot_signer(smart_contract_id, data.slot_id)` for the *target* contract and then verifies the chunk's signature against that address, again without binding to `smart_contract_id`: [4](#0-3) 

- This validation function is invoked from both the chunk-inventory/NACK "future view" buffering path in `handle_unsolicited_StackerDBPushChunk`: [5](#0-4) 

- and from the HTTP `POST /v2/stackerdb/:address/:contract/chunks` handler, which takes the `contract_identifier` purely from the URL path and forwards the (contract-agnostic) chunk signature straight into `try_replace_chunk`: [6](#0-5) 

Because the signed payload never encodes which contract it is for, any chunk that was legitimately observed on the network for contract A (e.g. `.signers-1-5`) can be re-submitted — by an unprivileged relayer/attacker who merely observed the gossiped/pushed message, without possessing any private key — as a chunk for contract B (e.g. `.signers-1-6`, or any other StackerDB whose slot table happens to assign the same signer address to the same `slot_id`), as long as B's current `slot_version` for that slot is lower than the replayed `slot_version` and does not exceed `max_writes`. The equality that should be enforced ("this signature authorizes a write to *this* StackerDB replica") degrades to "this signature authorizes a write to *any* replica with a matching slot/signer/version," which is exactly the class of stale/wrong-context-data-accepted-as-fresh flaw described in the oracle report (data validated as fresh/authentic for one context is silently reused as if valid in another).

### Impact Explanation
This allows an unauthorized third party (any peer that can observe or relay StackerDB gossip) to inject stale, out-of-context, but validly-signed data into a StackerDB replica that was never intended to receive it, as long as a slot/signer/version coincidence exists across contracts. Since StackerDB is used for consensus-adjacent signer coordination data (e.g. Nakamoto signer messages), this is an unauthorized write of forged/stale data into a StackerDB replica achieved without the signer's key, satisfying the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" impact categories, since accepted chunks are further rebroadcast via `set_relay_message`/gossip to other peers.

### Likelihood Explanation
Exploitability depends on there existing two StackerDB contracts (now or in the future) where the same signer address is assigned the same `slot_id`, which is plausible given how signer-slot assignment is derived deterministically from the signer set per reward cycle (adjacent/overlapping signer sets commonly retain the same relative ordering, and thus the same `slot_id`, across cycles). No code in the verification path defends against this even in principle, since the digest structurally cannot distinguish contracts. The attack requires no privileged access — only the ability to capture or witness a previously broadcast, validly-signed chunk and resubmit/replay it against a different contract endpoint or via P2P push.

### Recommendation
Include the target `QualifiedContractIdentifier` (issuer + contract name) as part of the signed digest in `SlotMetadata::auth_digest`, so that a chunk signature is cryptographically bound to the specific StackerDB replica it was authorized for. This requires a coordinated protocol/versioning change to `libstackerdb`'s `SlotMetadata`/`StackerDBChunkData` signing scheme and all call sites (`try_replace_chunk`, `validate_received_chunk`, HTTP and P2P handlers) to pass and check the contract identifier as part of verification.

### Proof of Concept
1. Node operator runs two StackerDB contracts, A and B, both assigning signer address `S` (private key held by the legitimate signer, not the attacker) to `slot_id = 0`.
2. Signer `S` signs and broadcasts (or POSTs) a chunk for contract A: `StackerDBChunkData { slot_id: 0, slot_version: 5, data, sig }`, valid per `SlotMetadata::verify` against A's slot 0 owner.
3. An attacker who merely observes this chunk on the network (via gossip or by querying contract A's chunk endpoint) re-submits the identical `(slot_id, slot_version, data, sig)` tuple to contract B's `POST /v2/stackerdb/<B-address>/<B-contract>/chunks` endpoint, or replays it as a `StackerDBPushChunkData` with `contract_id = B`.
4. `try_replace_chunk`/`validate_received_chunk` for contract B look up `S` as slot 0's expected signer for B, call `verify()`, which succeeds because the digest never referenced contract A or B — the chunk is accepted and stored under contract B, and further relayed to B's other replicas, despite never having been authorized for B.

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
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

**File:** stackslib/src/net/stackerdb/mod.rs (L784-792)
```rust
                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
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

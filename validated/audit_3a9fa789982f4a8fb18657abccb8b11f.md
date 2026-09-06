### Title
Cross-StackerDB chunk replay due to missing domain separator (contract identifier) in `SlotMetadata` signature - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest for a StackerDB chunk from only `slot_id`, `slot_version`, and `data_hash`. It never binds the signature to the specific StackerDB smart contract (`QualifiedContractIdentifier`) the chunk is meant for, nor to the network. This is the exact bug class described in the report: a signed message lacking a chain/domain identifier can be replayed unmodified in a different context where the same signature still verifies.

### Finding Description
The signing/verification digest is: [1](#0-0) 

Verification (`SlotMetadata::verify`) only checks that the recovered public key hash matches the expected `principal` — it has no notion of which StackerDB contract the chunk belongs to: [2](#0-1) 

The actual "which contract" binding is applied out-of-band, at the storage/API layer, not inside the signed data. `try_replace_chunk` looks up the expected signer for `(smart_contract, slot_id)` and then calls `slot_desc.verify(&slot_validation.signer)`, which only re-derives the digest from `slot_id`/`slot_version`/`data_hash`: [3](#0-2) 

The unauthenticated HTTP write endpoint takes the target contract purely from the URL path, and the submitted `StackerDBChunkData` JSON body carries no contract binding at all: [4](#0-3) [5](#0-4) 

On success, the node re-broadcasts the chunk to the rest of the network as a legitimate `StackerDBPushChunk` for that contract: [6](#0-5) 

The same missing binding is checked on the P2P unsolicited push path (`validate_received_chunk`), which again only verifies `(slot_id, slot_version, data_hash)` against the signer registered for *that* contract, and accepts if the version is fresh: [7](#0-6) 

Because the digest is `(slot_id, slot_version, data_hash)` only, any valid `(sig, slot_id, slot_version, data)` tuple that was produced for StackerDB contract A remains a valid signature for slot `slot_id` in any other StackerDB contract B, as long as the same signer address happens to occupy `slot_id` in contract B and B's local slot version for that slot is lower than the replayed `slot_version`. This is directly analogous to the `deployLPToken` case: the signed payload omits the equivalent of `chain.id` (here, the StackerDB/contract identity), so a message legitimately produced in one context can be authenticated as legitimate in a different context.

Signer StackerDB contracts are populated by slot assignments that are frequently correlated across contracts (e.g., separate signer-set contracts per reward cycle or per index range commonly preserve the same relative slot ordering for persisting signers), making address/slot_id collisions across distinct StackerDB contracts realistic rather than merely theoretical.

### Impact Explanation
Any unprivileged remote party who observes a validly-signed chunk for StackerDB contract A (chunks are gossiped/broadcast and independently retrievable) can resubmit the exact same `(slot_id, slot_version, sig, data)` tuple against a different StackerDB contract B via the unauthenticated `POST /v2/stackerdb/:address/:contract/chunks` endpoint. If contract B assigns the same signer address to that `slot_id`, the write is accepted as authentic for B and is then relayed network-wide as a legitimate `StackerDBPushChunk`, i.e., forged/mismatched data is written to state and propagated as canonical for a different contract than the signer intended. This matches the "unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data" impact class.

### Likelihood Explanation
Exploitation requires no privileged access: it only requires (a) obtaining an already-broadcast, validly-signed chunk (public information) and (b) a signer/slot_id collision across two live StackerDB contracts, which is plausible given how Stacks assigns slots by signer set ordering across related contracts. No secret key or admin role is needed — this is a pure replay of publicly observable signed data.

### Recommendation
Include a domain separator in the signed digest, such as the StackerDB contract's `QualifiedContractIdentifier` (and optionally the network/chain id), inside `SlotMetadata::auth_digest()`, and require it at both the P2P and HTTP validation paths (`try_replace_chunk`, `validate_received_chunk`). This binds a chunk's signature to the specific StackerDB instance it was created for, preventing cross-contract replay.

### Proof of Concept
1. Signer `S` owns `slot_id = 0` in StackerDB contract `A` and also owns `slot_id = 0` in StackerDB contract `B` (plausible if `A` and `B` are related signer-set contracts with overlapping/ordered membership).
2. `S` signs and pushes chunk `(slot_id=0, slot_version=5, data=D)` to contract `A` via `POST /v2/stackerdb/<A-addr>/<A-name>/chunks`; this is accepted and relayed.
3. Attacker (no keys, no privileges) intercepts this public chunk and resubmits the identical body to `POST /v2/stackerdb/<B-addr>/<B-name>/chunks`.
4. `RPCPostStackerDBChunkRequestHandler::try_handle_request` calls `try_replace_chunk` for contract `B`; `SlotMetadata::verify` succeeds because the digest never referenced contract `A`, and if `B`'s current slot version for slot 0 is `< 5`, the chunk is accepted, stored under `B`, and rebroadcast to the network as an authentic push for `B`.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L53-59)
```rust
    fn path_regex(&self) -> Regex {
        Regex::new(&format!(
            r#"^/v2/stackerdb/(?P<address>{})/(?P<contract>{})/chunks$"#,
            *STANDARD_PRINCIPAL_REGEX_STRING, *CONTRACT_NAME_REGEX_STRING
        ))
        .unwrap()
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

### Title
Cross-StackerDB chunk-signature replay due to `SlotMetadata::auth_digest` omitting the smart-contract identifier - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest` (and thus the signature checked in `StackerDBChunkData::verify`/`sign`) only commits to `(slot_id, slot_version, data_hash)`, never to the StackerDB's `smart_contract_id`. This mirrors the reported bug class: a security-relevant "equality" (what the signer actually authorized vs. what is checked at consumption time) is missing a field, so data validly signed for one context can be replayed as valid in a different context.

### Finding Description
`SlotMetadata::auth_digest` builds the signed digest from only three fields: [1](#0-0) 

This digest is what `SlotMetadata::sign`/`verify` operate on, and it's what `StackerDBChunkData::sign`/`verify`/`recover_pk` delegate to via `get_slot_metadata()`: [2](#0-1) 

The `smart_contract_id` (`QualifiedContractIdentifier`) that identifies *which* StackerDB the chunk belongs to is never part of the signed material. It is only supplied out-of-band by the verifier to look up the expected signer address:

- In P2P chunk validation, `smart_contract_id` is used solely to resolve `get_slot_signer(smart_contract_id, slot_id)`, and the signature itself is checked with `slot_metadata.verify(&addr)`, which never touches `smart_contract_id`: [3](#0-2) 

- In the HTTP write path (`POST /v2/stackerdb/:principal/:contract/chunks`), the contract id comes from the URL path and is passed to `try_replace_chunk`, but the actual authenticity check is `slot_desc.verify(&slot_validation.signer)` — again contract-agnostic: [4](#0-3) [5](#0-4) 

Because the signature never binds to the contract/StackerDB instance, if the same signer address is assigned to the same `slot_id` in two different StackerDB configurations (a common real-world situation, since signer sets for different Stacks StackerDBs — e.g., signer-coordination DBs across different reward-cycle contracts, or any two StackerDBs sharing a signer set — are frequently identical or overlapping), a signature the signer produced for a chunk in StackerDB A is *also* a valid signature for a same-shaped chunk (`slot_id`, `slot_version`, `data_hash`) in StackerDB B. An attacker who observes a broadcast/posted chunk for DB A can replay the identical `(slot_id, slot_version, sig, data)` tuple against DB B's `POST .../chunks` endpoint (or inject it via `StackerDBPushChunk`) and have it accepted as authentic, because every check along the path (`get_slot_signer`, `verify`) is satisfied without any contract-binding.

This is the direct analog of the reported issue: just as `IMultiSourceLoan.Loan.hash()` omitted `protocolFee` from the signed/committed hash — allowing a caller to supply an out-of-band, unauthenticated `protocolFee` that the equality check couldn't catch — `SlotMetadata::auth_digest` omits the StackerDB contract identifier from the signed digest, allowing an out-of-band, unauthenticated `smart_contract_id` to be paired with an otherwise-valid signature.

### Impact Explanation
This allows unauthorized **write** of attacker-replayed (but validly-signed-elsewhere) data into a StackerDB slot that the signer did not intend for that database, and/or network propagation of that replayed chunk via the push-chunk gossip path once accepted (`node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))` in `poststackerdbchunk.rs`). This matches the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" criteria. The severity depends on how often the same signer/slot pairing recurs across distinct StackerDB contracts in a deployed network; where it does, it is a genuine cross-context authentication bypass at the StackerDB layer.

### Likelihood Explanation
Exploitability requires: (1) an attacker to observe (not forge) one validly-signed chunk for slot `S` version `V` with a given signer in StackerDB A, and (2) the same signer address to also own slot `S` in StackerDB B with a version counter allowing acceptance of version `V`. Condition (1) is trivial (chunks are broadcast/gossiped publicly). Condition (2) depends on deployment/config of which signer sets are assigned to which slots across different StackerDB contracts — this is plausible in Stacks deployments where the same signer set services multiple StackerDBs, but I could not verify from the available code whether any specific production StackerDB configuration guarantees or forbids overlapping signer/slot assignment across contracts. This uncertainty should be resolved by inspecting how StackerDB configs are provisioned network-wide (out of the indexed context available to me).

### Recommendation
Include `smart_contract_id` (or a domain-separation tag derived from it) in `SlotMetadata::auth_digest`, so the signature commits to which StackerDB the chunk is destined for:

```rust
fn auth_digest(&self, smart_contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(smart_contract_id.to_string().as_bytes());
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
and thread `smart_contract_id` through `sign`/`verify` call sites (`StackerDBChunkData::sign/verify/recover_pk`, `try_replace_chunk`, `validate_received_chunk`), consistent with how the contract id is already available at every call site.

### Proof of Concept
1. Deploy two StackerDB contracts, DB-A and DB-B, both configuring slot `0` to be owned by the same signer address `addr` (a realistic scenario if the network reuses one signer set across multiple StackerDBs).
2. Signer signs `StackerDBChunkData { slot_id: 0, slot_version: 1, data }` for DB-A via `StackerDBChunkData::sign` — this only commits to `(0, 1, hash(data))`, per `SlotMetadata::auth_digest` [1](#0-0) .
3. Attacker observes this chunk on the wire (via P2P StackerDBPushChunk gossip) or fetches it from DB-A.
4. Attacker replays the identical bytes (`slot_id=0, slot_version=1, sig, data`) to `POST /v2/stackerdb/<DB-B-address>/<DB-B-contract>/chunks`.
5. `try_replace_chunk` resolves `slot_validation.signer` for DB-B slot 0 = `addr`, calls `slot_desc.verify(&addr)`, which succeeds because the signature never bound to DB-A vs DB-B [6](#0-5) . The chunk is accepted into DB-B's slot 0, and (if `accepted`) is relayed as `StackerDBPushChunk` to the network [7](#0-6) , even though the signer never authorized this write for DB-B.

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

**File:** libstackerdb/src/libstackerdb.rs (L213-244)
```rust
    /// Create an owned SlotMetadata describing the metadata of this slot.
    pub fn get_slot_metadata(&self) -> SlotMetadata {
        SlotMetadata {
            slot_id: self.slot_id,
            slot_version: self.slot_version,
            data_hash: self.data_hash(),
            signature: self.sig.clone(),
        }
    }

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-323)
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
```

### Title
StackerDB chunk signature omits the target contract identifier, enabling cross-database chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the digest that a slot owner signs over `slot_id`, `slot_version`, and `data_hash` only — it never includes the StackerDB's `smart_contract_id`. Because the write-path authorization (`get_slot_signer(smart_contract_id, slot_id)`) checks only that the recovered signer address owns that slot *in the contract being written to*, a signature produced for one StackerDB instance is cryptographically indistinguishable from a signature intended for any other StackerDB instance where the same address happens to own the same slot. Any relay/observer who has seen one valid, broadcast chunk can replay it verbatim into a different StackerDB contract, exactly analogous to the reported bug where a signature omitting the `implementation` parameter could be replayed against a different distribution implementation.

### Finding Description
The signing/verification digest is: [1](#0-0) 

which hashes only `slot_id`, `slot_version`, `data_hash` — the target StackerDB contract (the "implementation"/instance) is never part of what is signed: [2](#0-1) 

Authorization to write is enforced purely by looking up, per-contract, which address owns a given `slot_id`, and then checking the signature against that address: [3](#0-2) 

The unauthenticated HTTP write endpoint accepts any `(slot_id, slot_version, sig, data)` tuple for a caller-specified `contract_identifier`, verifies the signature via `try_replace_chunk`, and if accepted, re-broadcasts it via `StackerDBPushChunk` gossip to the whole network: [4](#0-3) [5](#0-4) 

Because the digest never binds to `smart_contract_id`, if the same public-key-hash is assigned the same `slot_id` in two different StackerDB contracts (this is normal/expected in this codebase — e.g. the `.signers-<cycle>-<lane>` boot contracts write the same ordered signer list into `stackerdb-set-signer-slots` across parallel per-lane contracts, and any third-party StackerDB app deployer can deliberately configure two contracts with identical slot→signer assignments), a chunk validly signed and accepted for contract A will also pass signature verification and be accepted (subject only to size/version/freshness checks, none of which reference the contract) for contract B. This breaks the equality "a signature authorizes a write to *this* StackerDB instance" — it actually only authorizes a write to *any* instance where the signer owns that slot index.

### Impact Explanation
An unprivileged network peer that merely observes one broadcast/posted chunk (chunks are gossiped in the clear, and posting is anonymous/unauthenticated at the HTTP layer — `security: []` on the endpoint) can re-post that exact `(slot_id, slot_version, sig, data)` to a different, unintended StackerDB contract on the same or other nodes, causing the node to accept it as a legitimate write and further relay it network-wide via `StackerDBPushChunk`. This is a forged-data propagation / cross-instance authorization bypass: content the signer only authorized for contract A is stored and gossiped as authentic content of contract B, without ever needing the signer's key. This can corrupt or desynchronize application-level StackerDB state (e.g. wrong-lane signer messages, mixed miner/signer content if slot layouts coincide), and is achievable by any network participant with zero privileges, matching "network-wide propagation of forged data" / "unauthenticated write to StackerDB".

### Likelihood Explanation
High for any deployment with more than one StackerDB contract sharing slot/address assignments (a realistic and even encouraged pattern, since the protocol itself provisions parallel per-lane `.signers-<cycle>-<n>` contracts with identical signer orderings, and third-party StackerDB apps are free to duplicate slot assignments across contracts). The attack requires no cryptographic work — only observing one gossiped/posted chunk and re-submitting it to a different `/v2/stackerdb/{addr}/{contract}/chunks` endpoint or wrapping it in a `StackerDBPushChunkData` with a different `contract_id`.

### Recommendation
Include the target `smart_contract_id` (or a domain-separated identifier of it) in `SlotMetadata::auth_digest()` so that a signature is bound to one specific StackerDB instance, mirroring the reported fix of adding the `implementation` parameter into the signed digest:
```rust
fn auth_digest(&self, smart_contract_id: &QualifiedContractIdentifier) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(smart_contract_id.serialize_to_vec()); // or similar canonical encoding
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
This requires threading the contract identifier through `sign`/`verify`/`recover_pk` and all call sites (`try_replace_chunk`, `validate_received_chunk`, `poststackerdbchunk.rs`, `libsigner` event parsing), which is a protocol-visible/wire-breaking change and needs careful, versioned rollout.

### Proof of Concept
1. Configure (or observe an existing deployment of) two StackerDB contracts, A and B, such that the same signer address owns `slot_id = 0` in both (this is realistic for the protocol's own parallel `.signers-<cycle>-<lane>` contracts, and trivially arrangeable for any third-party StackerDB app).
2. The legitimate signer signs and posts chunk `(slot_id=0, slot_version=1, data=D)` to contract A via `POST /v2/stackerdb/{addr}/{A}/chunks`; the node accepts it (`try_replace_chunk` succeeds because `SlotMetadata::verify` only checks `slot_id, slot_version, data_hash`) and relays it as `StackerDBPushChunkData{contract_id: A, chunk_data}`: [6](#0-5) 
3. Any peer that received this gossip (or the original poster) extracts `chunk_data` and re-submits the identical `(slot_id=0, slot_version, sig, data)` to `POST /v2/stackerdb/{addr}/{B}/chunks`, or crafts `StackerDBPushChunkData{contract_id: B, chunk_data}` and relays it.
4. Because `auth_digest()` never included the contract identifier, `SlotMetadata::verify` in `try_replace_chunk`/`validate_received_chunk` succeeds for contract B as well, and the chunk is stored and re-gossiped under contract B — without the attacker ever possessing the signer's private key and without the signer ever intending that data for contract B. [7](#0-6) [3](#0-2)

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L308-324)
```rust
        let ack_resp = match ack_resp {
            Ok(ack) => ack,
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

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

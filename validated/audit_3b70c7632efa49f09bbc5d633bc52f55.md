Confirmed: both `.signers-0-xxx` and `.signers-1-xxx` StackerDB contracts derive their signer-slot layout from the *same* `.signers` contract call (`stackerdb-get-signer-slots-page u0`, etc.), meaning the same signer address occupies the same `slot_id` across multiple distinct StackerDB contracts (e.g., message-type-0 DB and message-type-1 DB for the same reward cycle, and across reward cycles' `.signers-0-N`/`.signers-1-N` instances). [1](#0-0) 

### Title
Cross-StackerDB chunk replay due to signature not binding to contract identifier - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest that a signer's `MessageSignature` authenticates — only commits to `slot_id`, `slot_version`, and `data_hash`. It never commits to the `QualifiedContractIdentifier` of the StackerDB the chunk is destined for. [2](#0-1) 

### Finding Description
`StackerDBChunkData::sign`/`verify` and `SlotMetadata::sign`/`verify` compute the signed digest solely from `slot_id`, `slot_version`, and the SHA512/256 hash of the chunk bytes: [3](#0-2) 

The write path (`try_replace_chunk` in the DB layer, and `validate_received_chunk` in the network layer) fetches the *expected signer address for that particular contract's slot* and calls `slot_metadata.verify(&addr)`, which recomputes the same contract-agnostic digest and checks the ECDSA-recovered public-key hash against the expected address: [4](#0-3) [5](#0-4) 

Because the digest never binds to the contract, a signature that is valid for slot `S` in contract `A` is *also* valid for slot `S` in contract `B`, provided the same signer address owns slot `S` in `B`. This is not a hypothetical: the boot contracts `.signers-0-xxx` and `.signers-1-xxx` (one per Nakamoto message type, per reward cycle) all derive their slot layout from the same underlying `.signers` contract, so a given signer occupies the *same* `slot_id` across multiple, independently-replicated StackerDB instances. [1](#0-0) [6](#0-5) 

This is the same class of failure as the GovernorAlpha analog: the authenticity check enforces an *incomplete* binding (it doesn't restrict/scope the signature to the specific target it's meant for), so an attacker can present material that legitimately passes the check but was authorized for a different destination.

An unprivileged network observer who captures a chunk push/response for contract `A`, slot `S`, version `V` (`StackerDBChunkData{slot_id: S, slot_version: V, sig, data}`) can resubmit that exact byte-identical chunk to contract `B`'s slot `S` (via the p2p `StackerDBPushChunk` unsolicited-message path handled in `PeerNetwork::handle_unsolicited_StackerDBPushChunk`, or via the HTTP `POST /v2/stackerdb/.../chunks` RPC) as long as `B`'s expected version for slot `S` is `< V` (or `A` and `B` share write-count/version state coincidentally). `validate_received_chunk`/`try_replace_chunk` will accept it because the signature verifies against the same signer address that legitimately owns slot `S` in `B`. [7](#0-6) 

### Impact Explanation
This allows an unauthenticated third party (anyone who can observe the gossiped/relayed chunk, which travels in cleartext over the p2p wire) to write a signer's message — verbatim, but originally intended for a different StackerDB/message-type/reward-cycle — into a StackerDB slot it was not authored for. This is an unauthorized write into StackerDB state via forged-context propagation: the data was authentic *for a different contract* but is accepted as authentic for the target contract, corrupting the target DB's state without the signer having ever signed anything for that context. Depending on which contract is targeted (e.g. mixing a `BlockResponse` intended for one signer-message-type DB into another), this can mislead downstream consumers (e.g. `StackerDBListener` in `stacks-node`) that read from the wrong-context slot and act on stale/misattributed data as if it were fresh for that DB.

### Likelihood Explanation
High reachability: the attacker needs no keys and no special privileges — only the ability to observe a chunk once broadcast (StackerDB gossip is not encrypted/private) and to open any p2p connection or HTTP RPC session to a victim node to replay it into a differently-scoped StackerDB. The condition (same signer owning the same `slot_id` across contracts) is realistic and structurally guaranteed for the signer-message StackerDBs, which share slot layout by construction.

### Recommendation
Bind the signed digest to the target StackerDB's identity, not just to slot/version/data. Include the `QualifiedContractIdentifier` (and, for defense in depth, the network/consensus context such as `rc_consensus_hash`) in `SlotMetadata::auth_digest()`, and thread the contract id into `sign`/`verify` call sites (`StackerDBChunkData::sign`, `get_slot_metadata`, `try_replace_chunk`, `validate_received_chunk`). This closes the gap analogous to explicitly checking the vetoed function selector rather than trusting an incomplete equality.

### Proof of Concept
1. Node has slot `0` in `.signers-0-1` and `.signers-1-1` (or any two live StackerDB contracts) both owned by signer address `X` (guaranteed by shared `.signers` contract slot-layout, as shown in `signers-0-xxx.clar`/`signers-1-xxx.clar`).
2. Signer `X` legitimately produces `StackerDBChunkData{slot_id:0, slot_version:5, sig, data}` and it is gossiped/pushed for contract `.signers-0-1`.
3. Attacker (no keys required) intercepts this on the wire and constructs a `StackerDBPushChunkData{contract_id: .signers-1-1, rc_consensus_hash, chunk_data: <same struct>}`.
4. Sends it via p2p to a victim node; `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` looks up `.signers-1-1`'s slot-0 signer (also `X`), calls `slot_metadata.verify(&X)`, which succeeds because the digest never referenced the contract id — the chunk is accepted and stored into `.signers-1-1` slot 0, version 5, even though `X` never signed anything for `.signers-1-1`. [8](#0-7) [9](#0-8)

### Citations

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-717)
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

        Ok(true)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L742-792)
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
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

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

**File:** stackslib/src/net/stackerdb/config.rs (L205-216)
```rust
    fn eval_signer_slots(
        chainstate: &mut StacksChainState,
        burn_dbconn: &dyn BurnStateDB,
        contract_id: &QualifiedContractIdentifier,
        tip: &StacksBlockId,
    ) -> Result<Vec<(StacksAddress, u32)>, NetError> {
        let value = chainstate.eval_read_only(
            burn_dbconn,
            tip,
            contract_id,
            &format!("({STACKERDB_SLOTS_FUNCTION})"),
        )?;
```

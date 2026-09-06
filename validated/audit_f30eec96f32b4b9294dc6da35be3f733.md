### Title
StackerDB chunk signatures omit the target contract identifier, permitting cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the digest that a StackerDB slot owner signs over `slot_id`, `slot_version`, and `data_hash` only — it never includes the StackerDB smart-contract identifier that the chunk is destined for. Every place that accepts a chunk (`PeerNetwork::validate_received_chunk` for gossip/pushed chunks, and `StackerDBTx::try_replace_chunk` for the RPC/HTTP write path) looks up the expected signer for the *target* `contract_id` supplied out-of-band by the caller/URL, then only checks that the signature recovers to that address. Because the contract identifier is never part of what was actually signed, a valid, previously-observed chunk signed by an address for StackerDB contract A can be replayed — by anyone who observed it, no private key required — against a *different* StackerDB contract B, as long as that same address happens to be the slot owner of the same `slot_id` in contract B and the version checks pass. This is exactly the bug class in the external report: the signed authorization is missing a binding parameter (there, "max bond"; here, "target contract"), so a signature produced under one context can be repurposed under another context the signer never agreed to.

### Finding Description
The digest that is signed is: [1](#0-0) 

Note it hashes only `slot_id`, `slot_version`, and `data_hash` — no `contract_id`/`QualifiedContractIdentifier`, no chain/network binding.

Server-side validation for gossip/pushed chunks resolves the expected signer strictly from the caller-supplied `contract_id` and then just checks the (contract-agnostic) signature against that address: [2](#0-1) 

The `contract_id` used to select the expected signer comes from the outer `StackerDBPushChunkData` wrapper (attacker-controlled/relayed), not from anything cryptographically bound to the chunk's own signature: [3](#0-2) 

The same pattern exists on the unauthenticated HTTP write endpoint, where the target contract is taken from the URL path and the chunk (with its contract-agnostic signature) from the POST body — nothing ties the two together cryptographically: [4](#0-3) 

Consequently, if the same signer key/address owns the same `slot_id` in two different StackerDB replicas (a common situation in practice — e.g., signer sets across consecutive reward cycles reuse the same signing keys and the same relative slot allocation in `.signers-*` contracts, or any two application StackerDBs that both list the same operator as a slot owner), a chunk that was validly signed and broadcast/stored for contract A can be resubmitted (by any observer, not the signer) with contract B named instead, and will be accepted as authentic for contract B provided `slot_version`/`max_writes`/`chunk_size` checks pass in B. This breaks the "authenticated-for X" vs "stored-as-authentic-for Y" equality: the node ends up treating data as validly authored for a StackerDB the actual signer never authorized it for, and will further gossip/relay it to peers as legitimate content of contract B (`StackerDBChunkInv` update, further broadcast).

### Impact Explanation
This is an unauthenticated write/propagation issue reachable by any remote, unprivileged party who has observed one valid chunk (chunks are, by StackerDB design, freely readable/gossiped). It allows injecting content into a StackerDB contract that its slot owner never intended for that contract, and the network will then propagate this forged-context data as canonical for contract B. This matches the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" impact classes, since no admin/node-secret/private key of the target signer is needed — only that the same address is registered as a slot signer in both the source and target StackerDB configurations, and that a valid signed chunk for the source config has previously been observed.

### Likelihood Explanation
Requires that the same address owns the same `slot_id` in two different StackerDB configs that both have version/size states compatible with replay. Signer-set contracts commonly reuse the same operator addresses/slot indices across cycles, making this condition realistic rather than purely theoretical, and no cryptographic secret is needed by the attacker to carry out the replay — only observation and relay of previously broadcast/pushed chunk data.

### Recommendation
Include the target `contract_id` (and ideally the network/chain-id) as part of `SlotMetadata::auth_digest()`, so the slot owner's signature cryptographically commits to the specific StackerDB instance the chunk is meant for. Enforce this bound contract identifier check in `validate_received_chunk`/`try_replace_chunk` so that a signature computed for one StackerDB contract cannot be replayed as valid for another.

### Proof of Concept
1. Operator `S` (address `addr`) is configured as the owner of `slot_id = 0` in both StackerDB contract A and StackerDB contract B (e.g., two signer-set contracts across reward cycles with the same slot allocation).
2. `S` legitimately signs and pushes chunk `(slot_id=0, slot_version=5, data=D)` to contract A; this is observed on the p2p network by attacker `M` (no privileges needed — chunks are broadcast/gossiped).
3. `M` (not possessing `S`'s private key) constructs a `StackerDBPushChunkData` (or an HTTP POST to `/v2/stackerdb/{addr}/{contractB}/chunks`) using the *same* `StackerDBChunkData` bytes (same `sig`, `slot_id`, `slot_version`, `data`) but names contract B as the target.
4. Because `SlotMetadata::auth_digest()` (`libstackerdb/src/libstackerdb.rs` L160-166) never included contract identity in what was signed, `validate_received_chunk`/`try_replace_chunk` for contract B independently look up `addr` as B's slot-0 signer and successfully verify the (contract-agnostic) signature, accepting and storing/propagating the chunk as authentic content of contract B — despite `S` never having authorized this data for contract B.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-697)
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L53-93)
```rust
    fn path_regex(&self) -> Regex {
        Regex::new(&format!(
            r#"^/v2/stackerdb/(?P<address>{})/(?P<contract>{})/chunks$"#,
            *STANDARD_PRINCIPAL_REGEX_STRING, *CONTRACT_NAME_REGEX_STRING
        ))
        .unwrap()
    }

    fn metrics_identifier(&self) -> &str {
        "/v2/stackerdb/:principal/:contract_name/chunks"
    }

    /// Try to decode this request.
    /// There's nothing to load here, so just make sure the request is well-formed.
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-empty body".to_string(),
            ));
        }

        if preamble.get_content_length() > MAX_MESSAGE_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: PostStackerDBChunk body is too big".to_string(),
            ));
        }

        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        let chunk: StackerDBChunkData = serde_json::from_slice(body).map_err(Error::JsonError)?;

        self.contract_identifier = Some(contract_identifier);
        self.chunk = Some(chunk);

        Ok(HttpRequestContents::new().query_string(query))
    }
```

### Title
StackerDB chunk signatures are not bound to the target StackerDB (contract), enabling cross-database chunk replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
The signature that authorizes a write to a StackerDB slot (`SlotMetadata::auth_digest`) commits only to `slot_id`, `slot_version`, and `data_hash`. It never commits to the `QualifiedContractIdentifier` (the specific StackerDB instance) that the chunk is destined for. Because the HTTP endpoint and the p2p push-chunk validation path select "which StackerDB" purely from an out-of-band value (URL path / message field) that is *not* part of the signed digest, a valid chunk signature captured from one StackerDB replica can be replayed verbatim against a different StackerDB replica, as long as the signer happens to own the same `slot_id` there. This is structurally the same defect described in `CL-2021-40`: an authentication signature that does not depend on the verifier-specific "challenge" (here, the destination StackerDB identity) can be replayed outside its intended context.

### Finding Description
`SlotMetadata::auth_digest()` is the digest that gets signed and verified for every StackerDB write: [1](#0-0) 

Note it hashes only `slot_id`, `slot_version`, and `data_hash` — there is no contract/StackerDB identifier mixed in. `verify()` recovers the public key from this same context-free digest and compares its hash to the expected `principal`: [2](#0-1) 

Both places in `stackslib` that authorize a chunk write use this same digest without ever folding in the target contract identity:

- `StackerDBs::try_replace_chunk` looks up the expected signer *for that contract+slot*, but the actual cryptographic check (`slot_desc.verify(&slot_validation.signer)`) is over the contract-agnostic digest: [3](#0-2) 

- `StackerDBSync::validate_received_chunk` (used for both `handle_unsolicited_StackerDBPushChunk` on the p2p relay path and downloaded-chunk validation) does the same: it fetches the expected signer for `(smart_contract_id, slot_id)` and then verifies against the contract-agnostic digest: [4](#0-3) 

- The HTTP write endpoint `POST /v2/stackerdb/:address/:contract/chunks` takes the target contract purely from the URL path, entirely independent from the signed payload: [5](#0-4) 

Because none of these validation paths mix the contract identifier into the signed digest, the same `(slot_id, slot_version, data, sig)` tuple that a legitimate signer produced for StackerDB **A** is a valid, verifiable chunk for StackerDB **B** whenever that signer is also assigned `slot_id` in **B** and B's stored slot version at that ID is lower than `slot_version`. Since Stacks assigns many StackerDB replicas per reward cycle (`.signers-<cycle>-<n>`, `.miners`, etc.) with similar/overlapping slot layouts, an attacker who merely observes a broadcast (chunks are gossiped, not secret) can capture a signed chunk and directly write it into a different StackerDB the node participates in — an unauthorized/forged write without ever needing the signer's private key.

### Impact Explanation
This breaks the equality "signature authorizes write to *this* StackerDB slot" vs. "signature authorizes write to slot X in *any* StackerDB where the signer owns slot X." It allows an unprivileged remote attacker to inject attacker-chosen, previously-observed data into a StackerDB instance it was never signed for, i.e. an unauthenticated/unauthorized write to StackerDB state and network-wide propagation of forged/misattributed data (chunks are gossiped further once accepted). This matches the "Critical" impact bar (unauthenticated/unauthorized write to StackerDB; network-wide propagation of forged data).

### Likelihood Explanation
Exploitation requires only: (1) observing one validly-signed chunk broadcast for some StackerDB slot (chunks are public gossip data, not secret), and (2) the same signer address being assigned the same `slot_id` in a second StackerDB contract that has a lower stored version at that slot. Given Stacks' StackerDB layout (per-cycle signer DBs, miners DB, etc., which are frequently generated deterministically from the same PoX/signer set and thus tend to reuse consistent slot orderings), this precondition is realistically satisfiable, especially across successive reward cycles' `.signers-N-*` contracts. No secret key material or privileged role is required by the attacker.

### Recommendation
Include the target `QualifiedContractIdentifier` (StackerDB identity) — and ideally a network/consensus-scoped value — as part of `SlotMetadata::auth_digest()`, so a chunk signature is only valid for the specific StackerDB it was produced for. This requires updating `sign()`/`verify()`/`auth_digest()` in `libstackerdb/src/libstackerdb.rs` to take the contract id as an additional input, and updating all callers (`StackerDBChunkData::sign`/`verify`, `try_replace_chunk`, `validate_received_chunk`, and the signer-facing code such as `send_miners_message` in `stacks-node/src/nakamoto_node/signer_coordinator.rs`) to pass it through. This is a wire/consensus-relevant format change and needs careful migration/versioning.

### Proof of Concept
1. Node runs two StackerDB replicas, contracts `A` and `B`, where address `S` is assigned `slot_id = 0` in both (plausible for consecutive `.signers-<cycle>-*` contracts with the same signer set ordering).
2. Signer `S` legitimately signs and pushes chunk `(slot_id=0, slot_version=1, data=D)` to StackerDB `A`. Its signature `sig` is broadcast over the p2p network and observable by any peer.
3. Attacker captures `(slot_id, slot_version, data, sig)` and issues `POST /v2/stackerdb/<S_address>/B/chunks` (or crafts a `StackerDBPushChunkData` p2p message) targeting contract `B` instead of `A`, using the *same* unmodified `(slot_id, slot_version, sig)` and `data=D`.
4. `try_replace_chunk`/`validate_received_chunk` for contract `B` look up `S` as the expected signer for slot 0 in `B`, compute `auth_digest()` (which is identical to the one computed for `A`, since it excludes contract identity), and the signature verifies successfully — the forged write to `B` is accepted even though `S` never authorized anything for `B`.

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

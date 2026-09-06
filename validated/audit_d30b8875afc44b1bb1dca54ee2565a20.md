### Title
StackerDB chunk signature does not bind to the target contract, enabling cross-StackerDB replay of a validly-signed chunk - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary

### Finding Description
A `StackerDBChunkData`/`SlotMetadata` signature authenticates only `(slot_id, slot_version, data_hash)`; it never binds to the `QualifiedContractIdentifier` (StackerDB replica) the chunk is destined for: [1](#0-0) 

`verify()` recovers the public key purely from this contract-agnostic digest and compares its hash to the expected slot owner: [2](#0-1) 

The write path that stores a chunk, `StackerDBTx::try_replace_chunk`, looks up the expected signer for `(smart_contract, slot_id)` and calls `slot_desc.verify(&slot_validation.signer)` — but the signature itself says nothing about which `smart_contract` it was produced for: [3](#0-2) 

The same signature/verify pair is reused for gossip-received chunks in `validate_received_chunk`: [4](#0-3) 

Consequently, if a StacksAddress `S` legitimately owns `slot_id = X` in *contract A* and also owns `slot_id = X` in *contract B* (a common situation, since the `.signers-0-*` / `.signers-1-*` reward-cycle contracts and the `.miners` contract slot allocations are derived from the same, often-recurring, sets of addresses — see `stackerdb-set-signer-slots` in the boot contract), any unprivileged network observer can take a `(slot_id, slot_version, sig, data)` tuple that `S` validly signed and broadcast/relayed for contract A, and resubmit that exact tuple against contract B. It will pass `verify()` because the digest never mentions the contract, and it will pass the freshness/version checks in `try_replace_chunk` as long as the target slot's local version in contract B is below `slot_version`.

The unauthenticated HTTP endpoint makes this trivially reachable by any remote party without any credential: [5](#0-4) 

The `contract` and `address` (i.e. the target StackerDB) are taken straight from the URL path and are entirely attacker-controlled; the `chunk` (slot_id/slot_version/sig/data) is taken from the POST body. Nothing ties the previously-observed signed chunk to the specific replica being written to.

### Impact Explanation
This is an unauthenticated write of attacker-chosen (but signer-produced) data into a StackerDB replica the signature was never intended to authorize, breaking the equality the protocol relies on: "a valid signature for slot X proves authorization to write to slot X in *this* StackerDB." An attacker who merely observes gossip traffic (no private key needed) can inject a chunk into the wrong contract's slot, corrupting that replica's view without the true signer's consent for that contract, and this then gets rebroadcast/gossiped further by `process_stacker_db_chunks`/`broadcast_message` to the whole network, propagating stale/incorrect data as if it were freshly and correctly authorized for the target contract. Given the rules' Critical bucket ("unauthenticated/unauthorized write to state or StackerDB", "network-wide propagation of forged data"), this qualifies as Critical.

### Likelihood Explanation
Reachable with a single unauthenticated HTTP POST to `/v2/stackerdb/:address/:contract/chunks`, requiring only a previously-observed valid `(slot_id, slot_version, sig, data)` chunk for a different contract where the same address occupies the same slot index — a realistic condition given how the boot contracts allocate slot indices from ordered address lists. No admin role, no target signer's private key, and no special timing race is needed; it works from any single node.

### Recommendation
Bind the signed digest to the specific StackerDB replica by including the `QualifiedContractIdentifier` (and ideally a network/chain identifier) inside `SlotMetadata::auth_digest()`, e.g. hashing `smart_contract.to_string()` (or a canonical serialization of it) together with `slot_id`, `slot_version`, and `data_hash`. This requires threading the contract identifier into `sign()`/`verify()` calls in `libstackerdb/src/libstackerdb.rs`, and updating all call sites (`try_replace_chunk` in `stackerdb/db.rs`, `validate_received_chunk` in `stackerdb/mod.rs`, and the HTTP/tests) accordingly so a chunk signed for one StackerDB cannot be replayed into another.

### Proof of Concept
1. Observe (via gossip, or by directly requesting `/v2/stackerdb/...`) a validly-signed chunk `(slot_id=X, slot_version=V, sig, data)` written by signer `S` into StackerDB contract A, where `S` owns slot `X` in contract A.
2. Confirm `S` also owns slot `X` in a different StackerDB contract B (e.g., another `.signers-N-*` contract for a different reward cycle, or `.miners` if the address ordering coincides), and that contract B's current version for slot `X` is `< V`.
3. Without possessing `S`'s private key, `POST` the same JSON body `{slot_id: X, slot_version: V, sig, data}` to `/v2/stackerdb/<address-of-B>/<contract-of-B>/chunks`.
4. `RPCPostStackerDBChunkRequestHandler` forwards this straight into `try_replace_chunk` against contract B; `slot_desc.verify(&slot_validation.signer)` succeeds (the digest never referenced contract A vs B), the version check passes since `V` is fresher, and the chunk is stored and then broadcast to the network as if `S` had authorized it for contract B.

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

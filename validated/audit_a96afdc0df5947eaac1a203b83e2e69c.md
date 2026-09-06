## Finding

### Title
Cross-StackerDB-instance chunk signature replay due to missing contract/context binding in `SlotMetadata` digest - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The signature that authenticates a StackerDB chunk write is computed only over `(slot_id, slot_version, data_hash)`. It does not commit to the StackerDB's smart-contract identifier (or any other context such as reward cycle or network id). Because many independent StackerDB replicas (e.g. the per-message-type "signer" contracts `signers-{set}-{message_id}`) assign the *same* signer to the *same* `slot_id`, a chunk that was validly signed and broadcast for one StackerDB instance can be replayed verbatim into a different StackerDB instance and will pass signature verification there too, even though the signer never authorized that write for that instance.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`SlotMetadata::verify()` and `StackerDBChunkData::verify()`/`recover_pk()` use exactly this digest to recover/validate the signer, with no reference to which contract (`smart_contract_id`) the chunk is destined for: [2](#0-1) [3](#0-2) 

The write path in `StackerDBTx::try_replace_chunk` looks up the expected signer *for the target contract* and calls `slot_desc.verify(&slot_validation.signer)`, but since the signed digest itself never encoded which contract it belonged to, any chunk signed for slot `X` in contract `A` verifies identically against slot `X` in contract `B` whenever the two contracts assign the same signer to slot `X`: [4](#0-3) 

The same missing binding is used on the network-facing validation path (`validate_received_chunk`) used both for `StackerDBGetChunkData` gossip replies and for unsolicited `StackerDBPushChunk` (including the not-yet-committed `FutureView` buffering branch), all of which only check `slot_id`→signer and `slot_version`, never the identity of the current contract vs. the contract the signature was made for: [5](#0-4) 

Critically, in the signer StackerDB subsystem, the slot assignment for a given signer set is identical across *every* message-type lane. The `.signers` boot contract stores one slot list per signer-set index (0 or 1) via `stackerdb-get-signer-slots-page`, and every `signers-{set}-{message_id}` contract (for `message_id` in `0..SIGNER_SLOTS_PER_USER`) simply proxies to that same page: [6](#0-5) [7](#0-6) 

This is explicitly verified in the test suite: the same `expected_stackerdb_slots` list is asserted for every `message_id` in `0..SIGNER_SLOTS_PER_USER` for a given `signer_set`: [8](#0-7) 

So a given signer occupies the *exact same* `slot_id` in `signers-0-0`, `signers-0-1`, `signers-0-2`, … simultaneously. A chunk that is validly signed and observed (chunks are broadcast on the p2p network and are also readable/writable via the unauthenticated public HTTP endpoint) for one lane's contract can be resubmitted, byte-for-byte (same `slot_id`, `slot_version`, `sig`, `data`), to the write endpoint for a different lane's contract and will pass `BadSigner` checking because the signature never bound itself to which contract it was for: [9](#0-8) [10](#0-9) 

The same replay works across reward cycles: since signer set 0/1 alternates every reward cycle (`cycle-mod`), a signer that occupies slot `X` in reward-cycle `N`'s `signers-0-1` contract will occupy the same slot `X` in reward-cycle `N+2`'s `signers-0-1` contract if the sorted-by-pubkey ordering is unchanged (a common case when the signer set is stable), letting an old signed chunk be replayed into the future StackerDB instance, since the fresh contract starts at slot version `0` (`NO_VERSION`), and any old chunk's `slot_version >= 1` satisfies the monotonic-version check.

### Impact Explanation
This breaks the intended equality "signature is only valid for the specific StackerDB slot/contract it was produced for." An unprivileged remote attacker who merely observes gossiped/publicly-fetchable StackerDB chunk traffic (no private key needed) can inject that already-signed data into a *different* StackerDB replica (a different message-type lane, or the same lane in a future/past reward cycle) where the same signer key occupies the same `slot_id`. This is an unauthenticated/unauthorized write to StackerDB state — data appears validly signed and is accepted/stored/relayed by nodes for a StackerDB instance the signer never intended to write to, and can further be relayed/broadcast network-wide by honest nodes that treat it as a legitimate new chunk. This matches the Critical impact class ("unauthenticated/unauthorized write to state or StackerDB, network-wide propagation of forged data").

### Likelihood Explanation
The precondition — the same signer occupying the identical `slot_id` across multiple simultaneously-existing StackerDB contracts — is not a rare edge case but the deterministic, tested behavior of the `.signers` boot contract for every reward cycle (confirmed by `signers_tests.rs`). Chunks are routinely broadcast/observable on the network and the write path (`/v2/stackerdb/.../chunks` or unsolicited `StackerDBPushChunk`) is reachable by any peer without authentication. No signer private key or elevated role is required — only replaying already-public bytes — making this readily exploitable.

### Recommendation
Include the target `smart_contract_id` (and ideally reward-cycle/consensus/network context) in `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB replica it was produced for, analogous to adding chain ID/contract address per EIP-712. This requires a wire-format/protocol version bump for `StackerDBChunkData`/`SlotMetadata` since existing signers must include the extra context field(s) when producing signatures, and validating nodes must check that field against the contract they are storing the chunk into.

### Proof of Concept
1. Observe a validly-signed `StackerDBChunkData { slot_id: X, slot_version: V, sig, data }` broadcast by signer S for contract `signers-0-1` (e.g., via the p2p network or `GET /v2/stackerdb/{addr}/signers-0-1/{slot_id}`).
2. Because slot assignment is identical for signer set 0 across all message lanes (`signers-0-0`, `signers-0-2`, `signers-0-3`, `signers-0-4`), signer S also owns slot `X` in `signers-0-2`.
3. Submit the exact same `{slot_id: X, slot_version: V, sig, data}` tuple via `POST /v2/stackerdb/{addr}/signers-0-2/chunks`.
4. `try_replace_chunk` calls `slot_desc.verify(&slot_validation.signer)` using only `(slot_id, slot_version, data_hash)` — since S also owns slot `X` in `signers-0-2` and the version condition is satisfiable (this is either the first write in that lane, or a higher-version replay), verification succeeds and the chunk (data intended for the other lane) is accepted into `signers-0-2`'s replica and gets relayed to other nodes.

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

**File:** libstackerdb/src/libstackerdb.rs (L233-244)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/chainstate/stacks/boot/signers-1-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 1.
;; The contract name indicates which -- it has the form `signers-1-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u1))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L277-342)
```rust
#[test]
fn signers_db_get_slots() {
    let stacker_1 = TestStacker::from_seed(&[3, 4]);
    let stacker_2 = TestStacker::from_seed(&[5, 6]);

    let (mut peer, test_signers, latest_block_id, _) = prepare_signers_test(
        function_name!(),
        vec![],
        &[stacker_1.clone(), stacker_2.clone()],
        None,
    );

    let private_key = peer.config.private_key.clone();

    let mut expected_signers: Vec<_> =
        [&stacker_1.signer_private_key, &stacker_2.signer_private_key]
            .iter()
            .map(|sk| {
                let pk = Secp256k1PublicKey::from_private(sk);
                let pk_bytes = pk.to_bytes_compressed();
                let signer_addr = StacksAddress::p2pkh(false, &pk);
                let stackerdb_entry = TupleData::from_data(vec![
                    (
                        ClarityName::from_literal("signer"),
                        PrincipalData::from(signer_addr).into(),
                    ),
                    (ClarityName::from_literal("num-slots"), Value::UInt(1)),
                ])
                .unwrap();
                (pk_bytes, stackerdb_entry)
            })
            .collect();

    // should be sorted by the pk bytes
    expected_signers.sort_by_key(|x| x.0.clone());
    let expected_stackerdb_slots = Value::cons_list_unsanitized(
        expected_signers
            .into_iter()
            .map(|(_pk, entry)| Value::from(entry))
            .collect(),
    )
    .unwrap();

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
}
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L47-93)
```rust
/// Decode the HTTP request
impl HttpRequest for RPCPostStackerDBChunkRequestHandler {
    fn verb(&self) -> &'static str {
        "POST"
    }

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

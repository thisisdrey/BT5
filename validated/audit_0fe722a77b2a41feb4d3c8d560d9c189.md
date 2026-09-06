This confirms the finding: the contract identifier is taken purely from the URL path (`/v2/stackerdb/{address}/{contract}/chunks`) at [1](#0-0) , completely independent of the signature over the chunk body, and the contract name for signer StackerDBs is derived only from `reward_cycle % 2` and `message_id` at [2](#0-1) , with every `signers-<set>-<message_id>` contract sharing the identical signer→slot ordering as proven by the test at [3](#0-2) .

### Title
Cross-contract StackerDB chunk replay via signature that omits the destination contract identifier - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash` — it never binds the signature to the StackerDB smart-contract (the "domain") that the chunk is destined for. [4](#0-3)  Because every `.signers-<set>-<message_id>` StackerDB contract for a given signer set shares the exact same signer→slot-index assignment (verified by `stackerdb-get-signer-slots-page`, which returns the identical page regardless of `message_id`) [5](#0-4) [3](#0-2) , a chunk legitimately signed by a signer for one message lane (e.g. `signers-0-1` / `BlockResponse`) is *also* a validly-signed chunk for a sibling contract (e.g. `signers-0-2` / `StateMachineUpdate`, or `signers-0-3` / `BlockPreCommit`) at the very same `slot_id`. This is the same class of bug as the referenced report: a signature scheme lacking a domain separator (contract/chain binding), enabling replay across different "projects" (here, different StackerDB contracts/message lanes).

### Finding Description
`StackerDBChunkData`/`SlotMetadata` are authenticated only over `(slot_id, slot_version, data_hash)`: [6](#0-5) [7](#0-6) 

The contract that a chunk is written to is determined entirely out-of-band of the signature:
- Over HTTP, the contract comes from the URL path segments `address`/`contract`, decoded independently of the signed body: [8](#0-7) 
- Over P2P, `StackerDBPushChunkData.contract_id` is a separate, unsigned envelope field alongside the signed `chunk_data`: [9](#0-8) 

Server-side validation (`validate_received_chunk` and `StackerDBTx::try_replace_chunk`) only checks: chunk size, that the recovered signer address matches the *slot owner in that particular contract*, and that the slot version is monotonically increasing — it never verifies that the signature was produced *for this contract*: [10](#0-9) 

For the `.signers-<reward_cycle % 2>-<message_id>` family of boot contracts, the slot-to-signer assignment page is identical across all `message_id` values within the same signer set (`stackerdb-get-signer-slots-page` keys only on the set index, not `message_id`): [11](#0-10)  and this is explicitly asserted by the existing test that iterates `message_id` 0..N and expects the *same* slot list for every contract in a signer set: [3](#0-2) . This means the exact same `(slot_id, slot_version, sig, data)` tuple that recovers to a valid slot owner in `signers-0-1` also recovers to a valid slot owner at the same `slot_id` in `signers-0-2`, `signers-0-3`, etc. — the equality the authenticator is supposed to enforce ("this data was authored by the slot owner *for this StackerDB*") silently degrades to "this data was authored by the slot owner *for some StackerDB with the same slot ordering*".

### Impact Explanation
Any unprivileged network participant who observes a legitimately signed chunk (via the unauthenticated `StackerDBChunkInv`/`StackerDBGetChunk` P2P protocol, `StackerDBPushChunk` gossip, or the public `GET /v2/stackerdb/.../chunks/...` RPC endpoint) can replay it — without possessing any private key — into a *different* sibling StackerDB contract belonging to the same signer set, as long as the target contract's current Lamport clock for that slot is lower than the replayed `slot_version`. This is an unauthorized write to StackerDB state: the resulting slot in the second contract now holds data the signer never authorized to be stored there, and its Lamport clock is advanced by a third party. Any client that reads a raw chunk from one of these contracts without doing the message-type/lane cross-check that `libsigner`'s `SignerEvent::TryFrom` performs (e.g. tooling in `stacks-signer/src/monitor_signers.rs` or `stacks-signer/src/client/stackerdb.rs` that reads specific slot IDs directly) can be fed forged/misplaced content this way, and the legitimate signer's ability to write lower-numbered versions to that slot/contract is permanently foreclosed once replayed.

### Likelihood Explanation
High feasibility: no secrets are required, only observation of any one legitimately gossiped/queried chunk from a sibling contract sharing the same signer-set slot ordering (which is the normal case for every reward cycle's `signers-<set>-*` contract family), and a single unauthenticated HTTP POST or P2P `StackerDBPushChunk` to the sibling contract.

### Recommendation
Bind the signed digest to the destination StackerDB contract (and ideally the network/chain id), analogous to adding an EIP-712-style domain separator: include the `QualifiedContractIdentifier` (and optionally the network id / reward cycle) inside `SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs`, and update all signing/verification call sites (`StackerDBChunkData::sign`/`verify`/`recover_pk`, and every producer such as `stacks-signer/src/client/stackerdb.rs`) accordingly so a chunk signed for one contract can never validate against another.

### Proof of Concept
1. Let signer `S` legitimately sign and push a chunk to `signers-0-1` (`BlockResponse`) at `slot_id = 5`, `slot_version = 7`, yielding `sig`, `data`.
2. An attacker observes this chunk (e.g., via `GET /v2/stackerdb/{addr}/signers-0-1/chunks/5`, or by capturing the `StackerDBPushChunk` gossip message).
3. The attacker constructs a `StackerDBChunkData { slot_id: 5, slot_version: 7, sig, data }` (byte-for-byte identical, or with `slot_version` bumped further since `sig` only covers the tuple sent) and POSTs it to `/v2/stackerdb/{addr}/signers-0-2/chunks` (the `StateMachineUpdate` contract), which shares the same slot-5 owner via `stackerdb-get-signer-slots-page`.
4. `validate_received_chunk`/`try_replace_chunk` for `signers-0-2` recovers the same address from `sig` (since the digest never included the contract) and, if `signers-0-2`'s slot-5 Lamport clock is currently `< 7`, accepts and stores it — a write to `signers-0-2` that signer `S` never produced a `signers-0-2`-scoped signature for. [12](#0-11)

### Citations

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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1073)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
    }
```

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-341)
```rust
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
```

**File:** libstackerdb/src/libstackerdb.rs (L70-100)
```rust
/// Slot metadata from the DB.
/// This is derived state from a StackerDBChunkData message.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SlotMetadata {
    /// Slot identifier (unique for each DB instance)
    pub slot_id: u32,
    /// Slot version (a lamport clock)
    pub slot_version: u32,
    /// data hash
    pub data_hash: Sha512Trunc256Sum,
    /// signature over the above
    pub signature: MessageSignature,
}

/// Stacker DB chunk (i.e. as a reply to a chunk request)
#[derive(Clone, PartialEq, Serialize, Deserialize)]
pub struct StackerDBChunkData {
    /// slot ID
    pub slot_id: u32,
    /// slot version (a lamport clock)
    pub slot_version: u32,
    /// signature from the stacker over (slot id, slot version, chunk sha512/256)
    pub sig: MessageSignature,
    /// the chunk data
    #[serde(
        serialize_with = "stackerdb_chunk_hex_serialize",
        deserialize_with = "stackerdb_chunk_hex_deserialize"
    )]
    pub data: Vec<u8>,
}

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L1-43)
```text
(define-data-var last-set-cycle uint u0)
(define-data-var stackerdb-signer-slots-0 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-data-var stackerdb-signer-slots-1 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-map cycle-set-height uint uint)
(define-constant MAX_WRITES u4294967295)
(define-constant CHUNK_SIZE (* u2 u1024 u1024))
(define-constant ERR_NO_SUCH_PAGE u1)
(define-constant ERR_CYCLE_NOT_SET u2)

(define-map cycle-signer-set uint (list 4000 { signer: principal, weight: uint }))

;; Called internally by the Stacks node.
;; Stores the stackerdb signer slots for a given reward cycle.
;; Since there is one stackerdb per signer message, the `num-slots` field will always be u1.
(define-private (stackerdb-set-signer-slots 
                   (signer-slots (list 4000 { signer: principal, num-slots: uint }))
                   (reward-cycle uint)
                   (set-at-height uint))
	(let ((cycle-mod (mod reward-cycle u2)))
        (map-set cycle-set-height reward-cycle set-at-height)
        (var-set last-set-cycle reward-cycle)
        (if (is-eq cycle-mod u0)
            (ok (var-set stackerdb-signer-slots-0 signer-slots))
            (ok (var-set stackerdb-signer-slots-1 signer-slots)))))

;; Called internally by the Stacks node.
;; Sets the list of signers and weights for a given reward cycle.
(define-private (set-signers
                 (reward-cycle uint)
                 (signers (list 4000 { signer: principal, weight: uint })))
     (begin
      (asserts! (is-eq (var-get last-set-cycle) reward-cycle) (err ERR_CYCLE_NOT_SET))
      (ok (map-set cycle-signer-set reward-cycle signers))))

;; Get the list of signers and weights for a given reward cycle.
(define-read-only (get-signers (cycle uint))
     (map-get? cycle-signer-set cycle))

;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/net/codec.rs (L2590-2599)
```rust
            StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                contract_id: QualifiedContractIdentifier::parse("SP8QPP8TVXYAXS1VFSERG978A6WKBF59NSYJQEMN.foo").unwrap(),
                rc_consensus_hash: ConsensusHash([0x01; 20]),
                chunk_data: StackerDBChunkData {
                    slot_id: 2,
                    slot_version: 3,
                    sig: MessageSignature::from_raw(&[0x44; 65]),
                    data: vec![0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff]
                }
            }),
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
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

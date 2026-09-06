## Title
Cross-contract StackerDB chunk signature replay due to missing domain separation in `SlotMetadata::auth_digest` - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest that is signed to authenticate every StackerDB chunk — binds only `slot_id`, `slot_version`, and `data_hash`. It omits the smart-contract identifier of the StackerDB the chunk is destined for. Because the Nakamoto `.signers-<set>-<message_id>` contracts all share the exact same slot-to-signer assignment for a given reward cycle/signer-set, a signature that is valid for a chunk posted to one signer message-lane contract (e.g. `BlockPreCommit`) is *also* a valid signature for the same `slot_id`/`slot_version`/`data` triple in a different lane contract (e.g. `BlockResponse`). Any network observer — without holding any private key — can replay a captured, validly-signed chunk from one lane's StackerDB into another lane's StackerDB, where it will pass all server-side checks and be stored and re-gossiped as authentic data for that other contract.

### Finding Description
The signing/verification logic lives in `libstackerdb/src/libstackerdb.rs`:

```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
``` [1](#0-0) 

`sign()` and `verify()` operate solely over this digest: [2](#0-1) 

Neither the `StackerDBChunkData` message nor `SlotMetadata` struct carries the `smart_contract_id` as a signed field — the contract identity is only supplied out-of-band as a function parameter by the caller.

Both write paths rely exclusively on this digest for authentication, with no additional binding to the target contract:
- HTTP write path: `try_replace_chunk` looks up the target contract's slot owner and calls `slot_desc.verify(&slot_validation.signer)`, `stackslib/src/net/stackerdb/db.rs` lines 398-438. [3](#0-2) 
- P2P push/sync path: `validate_received_chunk` performs the identical check — it fetches the slot signer *for the given `smart_contract_id`* and then verifies against the digest that says nothing about which contract it is for: `stackslib/src/net/stackerdb/mod.rs` lines 679-697. [4](#0-3) 

Critically, the slot-to-signer assignment is *identical* across all `.signers-<set>-<message_id>` contracts for a given reward cycle: each such contract simply reads the same page from the shared `.signers` contract's `stackerdb-signer-slots-0`/`stackerdb-signer-slots-1` variable:
```
(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))
``` [5](#0-4) [6](#0-5) 

This is confirmed by the boot-contract test, which shows the *same* signer ordering/slots returned for every `message_id` within a signer set: [7](#0-6) 

Consequently, signer `S` occupies the same `slot_id` in `signers-0-1` (`BlockResponse`), `signers-0-2` (`StateMachineUpdate`), and `signers-0-3` (`BlockPreCommit`), per `MessageSlotID`: [8](#0-7) 

Because `auth_digest` doesn't encode which of these contracts the chunk is for, a chunk `(slot_id, slot_version, sig, data)` legitimately broadcast by signer `S` for `signers-0-3` verifies equally well when submitted (via the same POST `/v2/stackerdb/.../chunks` handler, `stackslib/src/net/api/poststackerdbchunk.rs` lines 197-201) or pushed over P2P for `signers-0-1`, as long as slot X's current stored version in the target contract is lower than the replayed `slot_version`. [9](#0-8) 

The only downstream filtering of payload type vs. lane (`signer_message_payload_matches_lane`) happens later, inside `libsigner`'s event-translation layer that feeds the signer's application logic — it does not gate what gets stored in the node's StackerDB replica or what gets relayed to peers: [10](#0-9) 

This means the P2P/RPC storage and relay layer itself accepts and re-broadcasts the forged (wrong-lane) chunk as canonical data for the target contract before any higher-layer sanity check occurs.

### Impact Explanation
Any unprivileged network peer that observes one validly-signed StackerDB chunk for a given `slot_id`/`slot_version` from a signer can replay it into a *different* message-lane StackerDB contract that shares the same slot assignment, without needing the signer's private key. The forged chunk passes `BadSigner`/authenticity checks and is:
- Persisted via `try_replace_chunk`/`insert_chunk` as if it were legitimate data for that contract,
- Propagated network-wide through `handle_unsolicited_StackerDBPushChunk` and normal StackerDB sync/replication to all other replicas, which perform the identical (broken) check.

This is a "network-wide propagation of forged data" / unauthorized write into StackerDB state, since the data was never actually authenticated for that specific contract by its signer.

### Likelihood Explanation
High. No secret key or privileged role is required — only passive observation of one legitimately broadcast chunk (which is inherently public P2P/RPC gossip data) plus a normal RPC POST or an unsolicited P2P push to a different, publicly-known signer-message contract ID that shares the same slot assignment.

### Recommendation
Include the target `smart_contract_id` (or a stable numeric/contract discriminator) as part of the signed digest in `SlotMetadata::auth_digest()`, so that a signature is only valid for the specific StackerDB contract it was created for. This requires bumping the wire format/serialization of `SlotMetadata`/`StackerDBChunkData` (or introducing a separate signed domain tag) and updating all signer/verifier call sites accordingly.

### Proof of Concept
1. Signer `S` (holding slot 3 in both `signers-0-1` and `signers-0-3` for the active reward cycle) signs and posts a `BlockPreCommit` chunk `(slot_id=3, slot_version=5, data=D)` to `signers-0-3`; the node accepts it and relays it via `StackerDBPushChunk`.
2. An observer captures this `(slot_id, slot_version, sig, data)` tuple off the wire.
3. The observer POSTs the identical tuple to `/v2/stackerdb/<addr>/signers-0-1/chunks` (i.e., the `BlockResponse` contract), where slot 3's currently stored version is `< 5`.
4. `try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs` looks up `signers-0-1`'s slot-3 signer (also `S`, per shared slot assignment), calls `slot_desc.verify(&S)` using the *same* `auth_digest` (which never referenced `signers-0-3`), and the check passes — the wrong-lane data `D` is stored and re-gossiped as authentic `BlockResponse` data.

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

**File:** libstackerdb/src/libstackerdb.rs (L168-193)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-423)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
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

**File:** libsigner/src/v0/messages.rs (L68-96)
```rust
define_u8_enum!(
/// Enum representing the stackerdb message identifier: this is
///  the contract index in the signers contracts (i.e., X in signers-0-X)
MessageSlotID {
    /// Block Response message from signers
    BlockResponse = 1,
    /// Signer State Machine Update
    StateMachineUpdate = 2,
    /// Block Pre-commit message from signers before they commit to a block response
    BlockPreCommit = 3
});

define_u8_enum!(
/// Enum representing the slots used by the miner
MinerSlotID {
    /// Block proposal from the miner
    BlockProposal = 0,
    /// Block pushed from the miner
    BlockPushed = 1
});

impl MessageSlotIDTrait for MessageSlotID {
    fn stacker_db_contract(&self, mainnet: bool, reward_cycle: u64) -> QualifiedContractIdentifier {
        NakamotoSigners::make_signers_db_contract_id(reward_cycle, self.to_u32(), mainnet)
    }
    fn all() -> &'static [Self] {
        MessageSlotID::ALL
    }
}
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

**File:** libsigner/src/events.rs (L580-614)
```rust
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    let Ok(message) = read_next::<T, _>(&mut &chunk.data[..]) else {
                        warn!(
                            "Skipping signer chunk: payload deserialization failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    Some((chunk.slot_id, pk, message))
                })
                .collect();
```

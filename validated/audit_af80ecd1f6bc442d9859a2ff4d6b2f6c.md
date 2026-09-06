## Title
Missing domain separation in `SlotMetadata` signing digest enables cross-contract StackerDB chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
The StackerDB chunk-authentication digest computed by `SlotMetadata::auth_digest` binds only to `slot_id`, `slot_version`, and `data_hash`. It never binds to the StackerDB contract (`QualifiedContractIdentifier`) the chunk is destined for. Because a signer's `slot_id` is identical across all `.signers-<set>-<message_id>` contracts of a given reward cycle (they are all derived from the same shared signer-slot list in the `.signers` boot contract), a validly-signed chunk observed in one StackerDB contract can be replayed verbatim into a *different* StackerDB contract for the same signer/slot, and it will pass signature verification there too.

### Finding Description
`SlotMetadata::auth_digest` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`SlotMetadata::sign`/`verify` operate purely over this digest, with no contract/lane identifier mixed in: [2](#0-1) 

At the validation layer, `StackerDBs::validate_received_chunk` looks up the expected signer address *per contract* (`get_slot_signer(smart_contract_id, slot_id)`), but the actual cryptographic check (`slot_metadata.verify(&addr)`) never incorporates `smart_contract_id` itself — the contract is used only to select which address is expected, not to bind the signature to that specific contract: [3](#0-2) 

The same gap exists on the write-path validation in `StackerDBTx::try_replace_chunk` (exercised in tests via `chunk_data.sign(pk)` / `tx.try_replace_chunk`), and in the RPC handler `poststackerdbchunk.rs`, both of which ultimately rely on `SlotMetadata::verify`.

Critically, slot allocation is *shared* across all message-id lanes of a reward cycle: the `.signers` boot contract stores a single `stackerdb-signer-slots-(0|1)` list per signer-set parity, and every `.signers-<set>-<message_id>` contract (`BlockResponse=1`, `StateMachineUpdate=2`, `BlockPreCommit=3`) reads that same list via `stackerdb-get-signer-slots-page`: [4](#0-3) 

This is confirmed by the test that iterates `message_id in 0..SIGNER_SLOTS_PER_USER` and shows every contract returns the identical signer-slot ordering: [5](#0-4) 

and `MessageSlotID::stacker_db_contract` only changes the contract name index, not the underlying slot assignment logic: [6](#0-5) 

Consequently, for a given signer address and reward cycle, `slot_id` is identical in the `BlockResponse`, `StateMachineUpdate`, and `BlockPreCommit` StackerDB contracts. Since the signature digest never distinguishes between these three contracts, a signature produced for one lane's chunk (same `slot_id`/`slot_version`/`data_hash`) is indistinguishable from — and interchangeable with — a signature intended for a different lane.

### Impact Explanation
An unprivileged network observer who sees a legitimately signed `StackerDBChunkData` gossiped/served for one `.signers-X-Y` contract (e.g. `BlockPreCommit`) can resubmit the identical `(slot_id, slot_version, data, sig)` tuple to a *different* `.signers-X-Z` contract for the same reward cycle (e.g. `StateMachineUpdate`), either via the `POST /v2/stackerdb/.../chunks` RPC path (`poststackerdbchunk.rs`) or via unsolicited P2P `StackerDBPushChunk`/`StackerDBGetChunk` exchange. `validate_received_chunk`/`try_replace_chunk` will accept and store it, because signature verification only checks `slot_id`/`slot_version`/`data_hash` match, not which contract the signer actually authorized. The node will then treat this cross-lane data as authentically signed for that StackerDB and propagate it to peers (store-and-forward broadcast), i.e. **forged-context data is served/propagated as canonical for a StackerDB the signer never authorized it for** — a break of the authenticated-write invariant described in the StackerDB spec itself, which states writes "must be signed by the slot's public key hash's associated private key **in order to be stored**" without qualifying which StackerDB that signature is scoped to: [7](#0-6) 

Downstream `libsigner` consumers do filter by payload-type prefix per lane (`signer_message_payload_matches_lane`) in `TryFrom<StackerDBChunksEvent>`: [8](#0-7) 

but this filtering happens only in the higher-level signer event decoder, not at the StackerDB storage/replication layer itself — the chunk is still accepted, persisted, and gossiped as validly-signed replica data under the wrong contract, polluting that StackerDB's replicated state and inventory for all nodes that sync it.

### Likelihood Explanation
The attack requires no private key and no elevated privileges: any observer of gossiped StackerDB chunk data (which is broadcast to all replicating peers) can immediately replay it to a sibling contract for the same signer/slot in the same reward cycle. The only precondition — that slot allocation is identical across the three per-cycle message-id contracts — is unconditionally true by design of the `.signers` boot contract.

### Recommendation
Include the target `QualifiedContractIdentifier` (or at minimum the `MessageSlotID`/message-id lane and reward cycle) in `SlotMetadata::auth_digest`, so that a signature is cryptographically scoped to the specific StackerDB it was produced for. This requires threading the contract identity into `sign`/`verify` (and updating on-wire message structures if the contract ID isn't already implicitly available at verification time), consistent with EIP-712-style domain separation as referenced in the analogous report.

### Proof of Concept
1. For reward cycle `R`, signer address `S` occupies `slot_id = k` in both `.signers-<set>-1` (BlockResponse) and `.signers-<set>-3` (BlockPreCommit), per shared `stackerdb-signer-slots-<set>` allocation.
2. `S` legitimately signs and posts a `BlockPreCommit` chunk: `StackerDBChunkData { slot_id: k, slot_version: v, data: D, sig }` to `.signers-<set>-3`.
3. Attacker observes this chunk via P2P gossip / `GET` chunk RPC.
4. Attacker submits the identical `(slot_id=k, slot_version=v, data=D, sig)` tuple via `POST /v2/stackerdb/<set>-1/chunks` (the `StateMachineUpdate` or `BlockResponse` contract), matching `slot_metadata.verify(addr)` in `validate_received_chunk`/`try_replace_chunk` since the digest only covers `slot_id`,`slot_version`,`data_hash`.
5. The node accepts and stores `D` under the wrong contract's slot `k`, and gossips it further to peers replicating that StackerDB, as confirmed by the acceptance logic in `validate_received_chunk`: [9](#0-8) .

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

**File:** stackslib/src/net/stackerdb/mod.rs (L46-52)
```rust
/// Each slot has an associated Lamport clock, and an associated public key hash used to
/// authenticate writes.  The Lamport clock is used to identify the latest version of a slot's
/// chunk -- a node will replace an existing but stale copy of a chunk with a new chunk if its
/// Lamport clock has a strictly higher value.  The slot's metadata -- its ID, Lamport clock, and
/// data hash -- must be signed by the slot's public key hash's associated private key in order to
/// be stored.  The chunks themselves are ordered byte sequences with no mandatory internal
/// structure.
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L15-43)
```text
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

**File:** libsigner/src/events.rs (L580-596)
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
```

### Title
StackerDB chunk signatures are not bound to the target contract, enabling cross-lane/cross-contract signature replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest for a StackerDB chunk write as `hash(slot_id || slot_version || data_hash)` only [1](#0-0) . It never includes the target StackerDB smart contract identifier. Because the same signer address is assigned the same `slot_id` across multiple sibling StackerDB contracts (`signers-{0|1}-1`, `signers-{0|1}-2`, `signers-{0|1}-3` for `BlockResponse`, `StateMachineUpdate`, `BlockPreCommit` respectively) [2](#0-1) [3](#0-2) , a signature that a signer produced to authorize a chunk write in one contract/lane is also a valid signature for the same slot in a sibling contract/lane, since the verification path checks only `(slot_id, slot_version, data_hash)` against the recovered signer, never the contract itself.

### Finding Description
This is directly analogous to the reported bug: a commitment/signature that should be scoped to one specific context (a specific StackerDB contract instance) is not cryptographically tied to that context, so it can be "copied" and replayed into another context where the same principal happens to hold the same slot.

Concretely:
- `SlotMetadata::sign`/`verify` compute the digest from only `slot_id`, `slot_version`, and `data_hash` [4](#0-3) .
- `StackerDBs::try_replace_chunk` receives the target `smart_contract`, looks up that contract's slot signer via `get_slot_validation`, and then calls `slot_desc.verify(&slot_validation.signer)` — the contract identifier is used only to select which signer address to check against, but is never part of what's actually signed [5](#0-4) .
- The same holds for the p2p validation path `PeerNetwork::validate_received_chunk`, which likewise only checks `slot_metadata.verify(&addr)` for the given `smart_contract_id`/`slot_id` pair [6](#0-5) .
- The HTTP POST endpoint `/v2/stackerdb/:principal/:contract/chunks` takes the contract from the URL path and forwards the chunk (with its externally-supplied signature) straight to `try_replace_chunk` with no additional binding check [7](#0-6) .
- Signer slot assignment is per signer-set page (`signers-0-*` / `signers-1-*`), and the *same* underlying signer list (and hence identical `slot_id` per signer) is shared by every message-id contract for that signer set: `signers-0-1`, `signers-0-2`, `signers-0-3` (and `signers-1-*`) all read from the same `stackerdb-get-signer-slots-page` [8](#0-7) , confirmed by the test that shows the identical `expected_stackerdb_slots` list applying across all `message_id` values for a signer set [9](#0-8) .

As a result, any chunk a signer legitimately signs and pushes to e.g. `signers-0-1` (`BlockResponse` lane) is a bytes-for-bytes valid, freely-replayable write for the same signer's slot in `signers-0-2` (`StateMachineUpdate`) or `signers-0-3` (`BlockPreCommit`), since these contracts are separate StackerDB instances with separate slot-version counters. An unprivileged remote observer who captures such a chunk from public StackerDB gossip/GET endpoints can resubmit it (once) to the sibling contract's POST endpoint, and the write will be accepted as if the signer authorized it there.

### Impact Explanation
This breaks the intended equality "signed by the slot owner *for this StackerDB instance*" vs. what is actually enforced, "signed by the slot owner for *some* StackerDB instance where they hold the same slot." It permits an unprivileged remote attacker to force acceptance of attacker-chosen bytes (previously legitimately signed for another lane/contract) into a StackerDB slot the signer never authorized for that specific contract, and to have that forged write relayed network-wide via `handle_unsolicited_StackerDBPushChunk`/`StackerDBPushChunk` gossip [10](#0-9) . Downstream, `signer_message_payload_matches_lane` in `libsigner/src/events.rs` filters most cross-lane payload-type mismatches before they're interpreted as `SignerMessage`s [11](#0-10) , which limits — but does not eliminate — the practical blast radius: the type-prefix filter only distinguishes *message kind*, not *reward cycle* or slot-version freshness, so stale/old messages that share a payload-type/lane (or which reuse the same contract across different reward cycles where signer_set parity repeats) can still be replayed as apparently-fresh writes as long as the version-freshness check is satisfied. Regardless of downstream interpretation, the StackerDB write itself is an unauthorized/forged write to state that the signer's key never actually authorized for that target contract — matching the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" impact classes.

### Likelihood Explanation
StackerDB chunk contents and signatures are, by design, public and gossiped/queryable (`GET /v2/stackerdb/.../chunks`, p2p `StackerDBChunkInv`/`StackerDBPushChunk`), so an attacker does not need any privileged access or the signer's private key — only the ability to observe one legitimately signed chunk and resubmit it to a sibling contract endpoint. No cryptographic secret is required, no signer-decision logic needs to be broken, and the fault is a straightforward missing domain-separation field in `auth_digest`.

### Recommendation
Include the target StackerDB contract identifier (and ideally slot-owner identity redundancy is fine since signature already achieves that, but critically the contract/reward-cycle context) in `SlotMetadata::auth_digest()`, e.g. hash `smart_contract_id.to_string()` (or its serialized bytes) together with `slot_id`, `slot_version`, and `data_hash`, and thread the contract identifier through `sign`/`verify`/`get_slot_metadata` so a signature is cryptographically scoped to exactly one StackerDB instance. This mirrors the referenced fix (PR#1217) of binding the committer/context (voter address, timestamp, price identifier, round ID) into the commitment hash.

### Proof of Concept
1. Signer `S` holds `slot_id = k` in both `signers-0-1` (BlockResponse) and `signers-0-2` (StateMachineUpdate) for the active reward cycle (guaranteed by shared slot-assignment source, `stackerdb-get-signer-slots-page`).
2. `S` legitimately signs and posts a `BlockResponse` chunk `C = StackerDBChunkData{slot_id:k, slot_version:v, sig, data}` to `signers-0-1`; it is accepted and relayed via `StackerDBPushChunk` gossip, and also fetchable via `GET /v2/stackerdb/<addr>/signers-0-1/chunks`.
3. Attacker `A` (unprivileged, no keys) fetches `C`.
4. `A` computes `C.slot_version` compatible with `signers-0-2`'s current slot state (e.g. version `v' >= current+1`, still ≤ `max_writes`), and constructs a chunk with the same `sig`, `slot_id=k`, adjusted `slot_version`, and same `data` (or verifies the identical `v` also works if `signers-0-2`'s slot happens to be at a lower version).
5. `A` POSTs this chunk to `/v2/stackerdb/<addr>/signers-0-2/chunks`.
6. `validate_received_chunk`/`try_replace_chunk` verify `slot_metadata.verify(&addr)` using `auth_digest = hash(slot_id, slot_version, data_hash)` — since `slot_id`, `slot_version`, and `data_hash` are unchanged, the signature recovers correctly to `S`'s address for `signers-0-2` even though `S` never signed anything intending it for `signers-0-2`. The write is accepted as `accepted: true` and re-relayed via gossip, confirming the contract-unbound replay.

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

**File:** libsigner/src/v0/messages.rs (L68-78)
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
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1063)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
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

**File:** libsigner/src/events.rs (L583-596)
```rust
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

This confirms the vulnerability path. The `signers-{signer_set}-{message_id}` StackerDB contracts for the same reward-cycle parity (`signer_set = reward_cycle % 2`) all derive their slot ordering from the identical call to `.signers stackerdb-get-signer-slots-page`, as shown in `signers-0-xxx.clar` lines 4-5 and confirmed by the test at `stackslib/src/chainstate/stacks/boot/signers_tests.rs:320-341`, where every `signers-{set}-{message_id}` contract for the same signer_set returns the exact same slot ordering. This means the same signer address occupies the same `slot_id` across every message-lane contract (BlockResponse, Transactions, etc.) sharing that reward-cycle parity, and can occupy the same `slot_id` again in future reward cycles with the same parity — while `SlotMetadata::auth_digest()` at `libstackerdb/src/libstackerdb.rs:159-166` only commits to `slot_id`, `slot_version`, and `data_hash`, never the contract/lane/reward-cycle identity. This is the exact "authenticated vs. stored" equality break analogous to the Golom report: the signature is not scoped to the specific storage context it's meant to authorize.

### Title
Cross-lane/cross-cycle StackerDB chunk replay due to missing contract binding in SlotMetadata signature - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash` [1](#0-0) . Neither the smart-contract identifier nor the reward-cycle/message-lane context is included in what gets signed. `try_replace_chunk` in the StackerDB store only checks that the signature recovers to the recorded `slot_validation.signer` for that `(stackerdb_id, slot_id)` pair, and that the version is fresh [2](#0-1) ; it never verifies that the signature was produced *for this specific contract*. The unsolicited chunk push and sync-download validation paths (`validate_received_chunk`) perform the identical checks, using only `slot_id`/`slot_version`/`data_hash` from the chunk and the locally recorded signer address, again without any contract binding [3](#0-2) .

### Finding Description
All `signers-{signer_set}-{message_id}` StackerDB contracts sharing the same `signer_set = reward_cycle % 2` parity derive their slot-to-signer assignment from the exact same underlying data (`.signers stackerdb-get-signer-slots-page`) [4](#0-3) , confirmed by the test showing identical slot ordering across all `message_id` lanes for a given `signer_set` [5](#0-4) . Consequently, a given signer address holds the *same* `slot_id` in every message-lane contract for a given cycle parity, and generally recurs at similar or identical `slot_id` positions across different reward cycles of the same parity (since parity resets every other cycle and set membership/ordering is deterministic by stacked amount).

Because `SlotMetadata`'s signed digest never binds to `smart_contract_id`, a signature that a legitimate signer produced for `(slot_id=X, slot_version=V, data_hash=H)` in one StackerDB contract (e.g., `signers-0-0`, reward cycle N) remains a fully valid signature for `(slot_id=X, slot_version=V, data_hash=H)` in any *other* StackerDB contract where that address is recorded as the slot-X signer — e.g., `signers-0-1` (a different message lane, same parity) or `signers-0-0` two reward cycles later (same parity, potentially same slot assignment). An attacker who has observed a chunk `(slot_id, slot_version, data, sig)` broadcast for one contract can simply repost/relay it (via `POST /v2/stackerdb/.../chunks` or `StackerDBPushChunk` gossip) against the other contract; `try_replace_chunk`'s signer/version checks will pass as long as the recorded signer for that slot in the target contract matches, and the target's currently stored version is lower than `V` (trivially true for a freshly-instantiated StackerDB, or any lane that lags behind) [6](#0-5) .

The relay path that consumes stored chunks does have a secondary type-tag filter (`signer_message_payload_matches_lane`) when decoding into `SignerMessageV0`, invoked in `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` [7](#0-6) , which limits which payload *types* are accepted per lane — but this filter operates only on the already-stored chunk's leading byte, after the forged/replayed write has already succeeded and corrupted the target StackerDB slot content, and provides no protection against cross-reward-cycle replay within the same lane (where the type tag naturally matches).

### Impact Explanation
This breaks the authenticated-vs-stored equality: a chunk that was signed to authorize storage in StackerDB contract A is accepted and stored as authoritative in StackerDB contract B without the signer having ever authorized data for B. This is an unauthenticated/unauthorized write to state — the recorded signer address for slot X in B never actually produced or approved that specific write for B. In the worst case, this allows an attacker to force-inject a stale `BlockResponse` (e.g. a signature over an old/different block) that was legitimately signed for cycle N into cycle N+2's StackerDB slot (same parity), corrupting the current view other nodes and the `stacks-node`'s `StackerDBListener` build from that contract, and potentially misleading consumers about which blocks were actually approved by signers for the current cycle.

### Likelihood Explanation
Exploitation requires no privileged access: any unprivileged network peer that can observe a previously broadcast, validly-signed StackerDB chunk (chunks are gossiped/pushed to arbitrary peers) can replay it verbatim against a different StackerDB contract it can determine shares the same slot-to-signer assignment. Slot assignment for same-parity contracts is deterministic and publicly computable from on-chain PoX/stacking data, so the attacker does not need to guess; they can precompute which `signers-X-Y` contracts share slot ownership with the source contract, then wait for and replay any observed chunk.

### Recommendation
Include the `smart_contract_id` (and ideally the reward cycle) as part of `SlotMetadata::auth_digest()`, so that a produced signature is cryptographically bound to the specific StackerDB replica it was meant to authorize:

```rust
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.smart_contract_id.serialize_to_vec()); // new
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```
This requires threading `smart_contract_id` into `SlotMetadata`/`StackerDBChunkData` sign/verify call sites (`try_replace_chunk`, `validate_received_chunk`, the `poststackerdbchunk` HTTP handler, and signer-side signing code) so both signer and verifier compute the digest over the same contract-scoped context.

### Proof of Concept
1. Identify two StackerDB contracts sharing signer-set parity, e.g. `signers-0-0` (reward cycle N, BlockResponse lane) and `signers-0-1` (reward cycle N, Transactions lane), where address `S` is assigned `slot_id = 3` in both (guaranteed per `signers-0-xxx.clar`'s shared `stackerdb-get-signer-slots` call).
2. Observe (via P2P StackerDBPushChunk gossip or the public chunk-fetch RPC) a legitimately signed chunk from `S` stored in `signers-0-0` at slot 3, version 5: `(slot_id=3, slot_version=5, data=D, sig=SIG)`.
3. Craft a `StackerDBChunkData { slot_id: 3, slot_version: 5, data: D, sig: SIG }` and POST it (or push it via gossip) to `signers-0-1`, where slot 3's currently stored version is < 5.
4. `try_replace_chunk` recovers `SIG` against `auth_digest()` computed only from `(3, 5, hash(D))`, matches recorded signer `S` for slot 3 in `signers-0-1`, version check passes → chunk is accepted and stored in `signers-0-1`, even though `S` never signed anything for that contract.

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

**File:** stackslib/src/net/stackerdb/db.rs (L398-437)
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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
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

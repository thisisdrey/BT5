### Title
Cross-StackerDB replay: chunk signatures omit the StackerDB contract identifier, allowing valid chunks from one StackerDB to be replayed into another - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` — the digest signers actually sign over for every StackerDB chunk — commits only to `(slot_id, slot_version, data_hash)`. It never includes the StackerDB's `QualifiedContractIdentifier` (the on-chain "domain" that scopes the slot). Because several boot StackerDB contracts (`signers-{0,1}-{1,2,3}`, one per message lane per reward-cycle parity) assign the *same* signer address to the *same* `slot_id`, a chunk that a signer validly signed and published for one contract/lane can be replayed byte-for-byte into a different StackerDB contract where that signer also owns that slot — exactly the missing-domain-separator pattern described in the report (there `chain.id` was missing; here the StackerDB `contract_id` is missing).

### Finding Description
The signing/verification logic lives in `libstackerdb/src/libstackerdb.rs`: [1](#0-0) 

`auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`. `sign`/`verify` operate purely on this digest: [2](#0-1) 

The node-side acceptance path, `StackerDBs::validate_received_chunk` (used both for the `PUT /v2/stackerdb/.../chunks` RPC and for unsolicited P2P `StackerDBPushChunk` gossip), resolves the expected signer purely from `smart_contract_id` + `slot_id`, but the actual cryptographic check (`slot_metadata.verify(&addr)`) never binds the contract identity into what was signed: [3](#0-2) 

Meanwhile, multiple *distinct* boot StackerDB contracts intentionally reuse the exact same signer→slot_id mapping. The `.signers` contract stores one shared list per reward-cycle-parity, and every message-lane contract (`signers-0-1`, `signers-0-2`, `signers-0-3`, and their `signers-1-*` counterparts) just calls into that same shared page: [4](#0-3) [5](#0-4) 

This is confirmed by the test that shows the identical slot assignment being handed back for every `message_id` within a signer set: `signers_db_get_slots` in `stackslib/src/chainstate/stacks/boot/signers_tests.rs:277-341` iterates `message_id in 0..SIGNER_SLOTS_PER_USER` and asserts the same `expected_stackerdb_slots` for all of them.

The only thing that currently stops cross-lane confusion is an *application-layer* heuristic inside `libsigner`, not the storage/relay layer: `signer_message_payload_matches_lane` filters chunks by the first byte's `SignerMessageTypePrefix` when constructing a `SignerEvent`: [6](#0-5) 

That filter is applied only when a signer client decodes a `StackerDBChunksEvent` into a typed `SignerMessage`; it is not enforced by `StackerDBs::validate_received_chunk`, `StackerDBTx::try_replace_chunk`, the `POST /v2/stackerdb/.../chunks` handler, or the P2P `handle_unsolicited_StackerDBPushChunk` path. Those layers accept and durably store/gossip any chunk whose signature recovers to the slot's registered owner — regardless of which contract the bytes were originally signed for.

### Impact Explanation
An attacker who observes a validly-signed chunk published by signer `S` for slot `X` in contract `signers-0-1` (`BlockResponse` lane) — chunk data and signatures are public, served over both HTTP and P2P gossip — can immediately re-POST/re-push that exact `StackerDBChunkData` to `signers-0-2` (`StateMachineUpdate` lane) or `signers-0-3` (`BlockPreCommit` lane) for the same reward-cycle parity, since `S` owns the identical `slot_id` there too. `validate_received_chunk`/`try_replace_chunk` will accept it (signature verifies, version/size checks are per-contract and independent), causing the target StackerDB replica to durably store and gossip a `BlockResponse` payload under a slot that is supposed to hold `StateMachineUpdate`/`BlockPreCommit` data. This is non-canonical data being served as canonical StackerDB state for that slot/contract to any peer or RPC client that fetches it — the storage/relay layer has no way to distinguish "authentic for lane A" from "authentic for lane B" because the signature never bound the contract. This matches the in-scope "High" impact category (non-canonical state served as canonical) and requires no privileged access — only a previously observed, publicly-broadcast chunk and an unauthenticated `PUT`/gossip write.

### Likelihood Explanation
High likelihood in practice: chunks are inherently public (broadcast via P2P `StackerDBPushChunk` and readable via the open `GET /v2/stackerdb/.../chunks` RPC), the boot contracts deliberately reuse identical slot-to-signer mappings across lanes within a reward-cycle parity, and the write path (`POST /v2/stackerdb/.../chunks`) is unauthenticated aside from the chunk's own signature. No node secret, admin role, or other party's key is required — only replaying data that the legitimate signer already published.

### Recommendation
Include the StackerDB `QualifiedContractIdentifier` (and ideally a network/chain identifier) inside `SlotMetadata::auth_digest()` so signatures are bound to the specific StackerDB instance they were produced for, mirroring how `structured_data_message_hash`/domain separation is used elsewhere in the signer protocol. This requires a `StackerDBChunkData`/wire-format version bump and coordinated signer/node upgrade, since it changes what is being signed.

### Proof of Concept
1. Signer `S` normally publishes a `BlockResponse` chunk to `signers-0-1` slot `X`:
   `StackerDBChunkData::new(X, v, block_response_bytes)`, signed with `S`'s key — accepted per `libsigner/src/v0/messages.rs` `SignerMessageTypePrefix::BlockResponse` and stored via `try_replace_chunk`.
2. Attacker captures this `StackerDBChunkData { slot_id: X, slot_version: v, sig, data: block_response_bytes }` from the public P2P gossip or `GET /v2/stackerdb/<signers-0-1>/chunk/X`.
3. Attacker resubmits the identical struct via `POST /v2/stackerdb/<signers-0-2>/chunks` (the `StateMachineUpdate` lane), where `S` also owns slot `X` (per `signers_db_get_slots` test behavior).
4. `StackerDBs::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-717`) looks up `S` as the expected signer for `(signers-0-2, X)`, calls `slot_metadata.verify(&S)`, which succeeds because `auth_digest()` never referenced `signers-0-1` vs `signers-0-2`. The chunk (whose payload byte-prefix is `BlockResponse`, invalid for this lane) is stored and gossiped as the current chunk for `signers-0-2` slot `X`.
5. Any consumer reading `signers-0-2` slot `X` via the RPC/P2P storage layer receives this stale/foreign-lane data as the "latest" authenticated chunk for that slot, even though it was never intended for that contract.

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L40-43)
```text
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

**File:** libsigner/src/events.rs (L580-613)
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
```

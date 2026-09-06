### Title
StackerDB chunk signatures do not bind the target contract, enabling cross-lane chunk replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest()` — the digest that authenticates every StackerDB chunk write — commits only to `slot_id`, `slot_version`, and `data_hash`, but never to the StackerDB smart contract identifier the chunk is destined for. Because the `.signers-{0|1}-{message_id}` boot contracts all share the exact same signer→slot assignment for a given signer-set/reward-cycle, a validly-signed chunk broadcast for one message lane (contract) can be replayed verbatim into a sibling lane, and the node's own validation logic will accept it as authentic. This is directly analogous to the reported BIP-322 flaw: the signature scheme fails to bind a context field it should (SIGHASH type there; contract identity here), so a signature "valid" in one context is wrongly treated as valid in another.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`sign()`/`verify()`/`recover_pk()` operate solely on this digest, with no reference to the owning `QualifiedContractIdentifier`: [2](#0-1) [3](#0-2) 

The network-layer write-path validation, `PeerNetwork::validate_received_chunk`, looks up the expected signer address *for the specific `smart_contract_id` and `slot_id`* but then calls `slot_metadata.verify(&addr)`, which — as shown above — never checks that the signature was produced for this `smart_contract_id`: [4](#0-3) 

Critically, the Clarity-side `.signers` boot contract assigns the *same* slot-id → signer-address mapping to every `.signers-{set}-{message_id}` contract in a signer set — `stackerdb-get-signer-slots-page` returns identical data for all message-lane contracts sharing `signer_set`: [5](#0-4) 

This is confirmed by the test `signers_db_get_slots`, which asserts that for every `message_id in 0..SIGNER_SLOTS_PER_USER`, the same `signers_set` contract's slot list is identical: [6](#0-5) 

Because the same address occupies the same `slot_id` across all sibling lane contracts, and the signature never commits to the contract identifier, a chunk `(slot_id, slot_version, sig, data)` legitimately signed and broadcast by a signer for contract `signers-0-A` is a byte-for-byte valid, verifiable chunk for contract `signers-0-B` as well (same `slot_id`, and if the attacker chooses `slot_version`/`data` to satisfy `signers-0-B`'s freshness check). The `insert_chunk`/`try_replace_chunk` write path stores raw bytes keyed by `(stackerdb_id, slot_id)` with no cross-check against the contract the signature was originally produced for: [7](#0-6) 

The only place that filters messages by "lane"/message-type is an application-level heuristic in `libsigner`'s event conversion (`signer_message_payload_matches_lane`), which is used solely when the signer client interprets already-stored chunks — not when the StackerDB replica decides whether to accept and gossip a write: [8](#0-7) 

Thus the storage/replication layer — the thing that actually accepts writes and propagates them network-wide via `StackerDBPushChunk`/inventory sync — has no binding between "this signature was produced for lane X" and "this write is being stored/relayed in lane Y."

### Impact Explanation
Any unprivileged network observer can capture a validly-signed StackerDB chunk gossiped for one `.signers-{set}-{message_id}` contract and relay/replay it (unmodified `sig`, `slot_id`, `data`) as a purported write to a different sibling lane contract that has the identical slot/signer assignment. `validate_received_chunk` will accept it (signature recovers to the correct owner address for that slot in the target contract), causing the node to store and further gossip forged-context data as if it were freshly authorized for that lane. This is an unauthenticated write of state that was never actually authorized for that destination contract, and it propagates network-wide via the existing StackerDB chunk-push/sync protocol — matching the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" impact bar.

### Likelihood Explanation
Exploitation requires no privileged access or private key: an attacker only needs to observe one legitimately broadcast, validly signed chunk (chunks are gossiped in the clear over the p2p StackerDB sync protocol) and resend it against a different contract ID that shares the same signer-slot layout, which is guaranteed by construction for all `.signers-{set}-{message_id}` boot contracts. The reused `(slot_id, slot_version, data_hash)` triple must satisfy the target contract's own freshness (`expected_version`) check, which is trivially satisfiable by choosing a target lane whose expected version is still low, or by simply resubmitting an early-version chunk to a lane that hasn't yet advanced past that version.

### Recommendation
Bind the target `QualifiedContractIdentifier` (or an equivalent contract-scoped domain separator) into `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB contract it was produced for. This requires a coordinated protocol/version bump since it changes the signed payload format (analogous to enforcing `SIGHASH_ALL` binding in the referenced BIP-322 fix) — old and new nodes must agree on what is being signed.

### Proof of Concept
1. Assume two boot contracts `signers-0-3` and `signers-0-7` for the same signer set — per `signers.clar`'s `stackerdb-get-signer-slots-page`, both assign slot `0` to address `A`.
2. Signer `A` legitimately signs and pushes chunk `C = StackerDBChunkData{slot_id:0, slot_version:5, sig:S, data:D}` to `signers-0-3`; the p2p network gossips it (visible to any peer via `StackerDBPushChunk`).
3. Attacker (no keys required) intercepts `C` and resends it, unmodified, as a `StackerDBPushChunk` targeting `signers-0-7` (or via `POST /v2/stackerdb/.../chunks` if reachable).
4. `PeerNetwork::validate_received_chunk` for `signers-0-7` looks up `get_slot_signer(signers-0-7, 0)` → `A`, then calls `slot_metadata.verify(&A)`; since `auth_digest()` only covers `(0, 5, hash(D))`, verification succeeds even though `A` never signed anything for `signers-0-7`.
5. Provided `5` is `>= expected_version` for slot 0 on `signers-0-7`, the forged chunk is accepted, stored, and re-gossiped by the node as authentic data for `signers-0-7`.

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

**File:** libstackerdb/src/libstackerdb.rs (L171-193)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L371-396)
```rust
    /// Insert a chunk into the DB.
    /// It must be authenticated, and its lamport clock must be higher than the one that's already
    /// there.  These will not be checked.
    fn insert_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "UPDATE chunks SET version = ?1, data_hash = ?2, signature = ?3, data = ?4, write_time = ?5 WHERE stackerdb_id = ?6 AND slot_id = ?7";
        let mut stmt = self.sql_tx.prepare(sql)?;

        let args = params![
            slot_desc.slot_version,
            Sha512Trunc256Sum::from_data(chunk),
            slot_desc.signature,
            chunk,
            u64_to_sql(get_epoch_time_secs())?,
            stackerdb_id,
            slot_desc.slot_id,
        ];

        stmt.execute(args)?;
        Ok(())
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

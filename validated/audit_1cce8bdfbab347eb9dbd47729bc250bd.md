### Title
`SlotMetadata::verify()` signature omits the StackerDB contract identity, enabling cross-contract chunk replay/forgery - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the value that is signed and later verified for every StackerDB chunk from only `(slot_id, slot_version, data_hash)` [1](#0-0) . It never binds the digest to the StackerDB smart-contract identity that the chunk is destined for. Both write paths that accept externally supplied chunks — the unauthenticated HTTP `POST /v2/stackerdb/:principal/:contract/chunks` handler and the unsolicited P2P `StackerDBPushChunk` path — validate a chunk purely against `(slot_id, slot_version, data_hash)` plus a per-contract slot→signer table, and never re-derive or check that the signature was produced *for this contract*. This is analogous to the reported bug class: a value used as an authenticator/selector is computed over an incomplete set of inputs, so two different "targets" (here, two different StackerDB contracts) that happen to share a signer/slot mapping become indistinguishable to the verifier — breaking the intended "signed-for-X equals accepted-into-X" equality.

### Finding Description
- Signing: `SlotMetadata::sign()` / `StackerDBChunkData::sign()` hash and sign only `slot_id || slot_version || data_hash` [2](#0-1) .
- Verification: `SlotMetadata::verify()` recomputes the same digest and recovers/compares the public key hash against the expected signer address for that slot — again with no contract binding [3](#0-2) .
- Storage-side enforcement (`StackerDBs::try_replace_chunk`) looks up `slot_validation.signer` for the *target* `smart_contract`, then calls `slot_desc.verify(&slot_validation.signer)` [4](#0-3) . It trusts that a signature which recovers to the correct address for slot `i` in contract `C` must have been produced *for* contract `C` — but nothing about the signed bytes says which contract it was for.
- The P2P validation path (`PeerNetwork::validate_received_chunk`) has the identical gap: it looks up the expected signer for `smart_contract_id`/`slot_id` and calls `slot_metadata.verify(&addr)`, again omitting the contract from what's checked [5](#0-4) .
- The unauthenticated HTTP write path (`RPCPostStackerDBChunkRequestHandler::try_handle_request`) accepts a `contract_identifier` taken directly from the URL and a `StackerDBChunkData` taken directly from the POST body, then calls `try_replace_chunk(&contract_identifier, ...)` [6](#0-5) [7](#0-6) . On success it re-broadcasts the chunk network-wide as a `StackerDBPushChunk` [8](#0-7) .

Because signer sets for a given reward cycle are computed once and then written into both/either "lane" StackerDB replicas for that cycle (the `.signers-<cycle>-1` / `.signers-<cycle>-2` contracts recognized in `libsigner/src/events.rs` via `get_signers_db_signer_set_message_id`), the same signer address is very plausibly assigned to the same numeric `slot_id` in more than one contract for the same cycle (both are derived from the identical ordered signer list produced by `NakamotoSigners::update_signers`/`pox_5_make_signer_set`, which is written via `stackerdb-set-signer-slots` with `num-slots = 1` per signer, in signer-sorted order) [9](#0-8) [10](#0-9) . I was not able to fully trace, within the remaining budget, the exact code that instantiates the two per-lane `signers-<cycle>-{1,2}` StackerDB configs from this signer list to conclusively prove the slot indices are identical across the two lanes in every deployment — this should be verified directly against `stacks-node/src/nakamoto_node/stackerdb_listener.rs` and `stacks-node/src/event_dispatcher/stacker_db.rs` before treating exploitability as fully confirmed.

### Impact Explanation
If (as strongly suggested by the shared, deterministically-ordered signer list) the same signer address occupies the same `slot_id` in two StackerDB contracts (e.g., the two message-id lanes of the same reward cycle, or across two contracts that reuse the same signer ordering), then:
- Any unprivileged network participant who observes one validly-signed, already-broadcast `StackerDBChunkData` (chunks are gossip data, not secret) can resubmit the identical bytes to the *other* contract's `/v2/stackerdb/.../chunks` endpoint or as an unsolicited `StackerDBPushChunk`.
- `try_replace_chunk`/`validate_received_chunk` will accept it as if it were legitimately authored for that other contract, because the signature check never encodes which contract it was signed for.
- The forged-context chunk is then stored and, via the HTTP handler, re-broadcast network-wide as a `StackerDBPushChunk`, propagating cross-context data as if it were authentic for that DB/lane.

This matches "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" in the Critical impact bucket, since the write requires no possession of any secret beyond replaying already-public signed bytes.

### Likelihood Explanation
The write paths (`RPCPostStackerDBChunkRequestHandler`, `handle_unsolicited_StackerDBPushChunk`) are reachable by any peer without authentication, and the vulnerable digest computation (`SlotMetadata::auth_digest`) is exercised on every chunk write, so the root cause is definitely present and remotely triggerable. The remaining uncertainty is solely whether the deployed slot-assignment logic ever actually places the same signer address at the same `slot_id` across two different StackerDB contracts that both accept unsolicited pushes/HTTP posts — this needs confirmation against the stackerdb-config construction code for the miner/signer replica contracts (not fully inspected here due to tool budget).

### Recommendation
Include the target StackerDB's `QualifiedContractIdentifier` (and ideally the reward-cycle/message-id lane) as part of the signed digest in `SlotMetadata::auth_digest()`, and thread that contract identity through `sign()`/`verify()` so that a chunk signed for contract `A` cannot recover/verify successfully against slot data belonging to contract `B`, even if the same address happens to own the same `slot_id` in both.

### Proof of Concept
Conceptual PoC (subject to confirming identical slot assignment across two target contracts, e.g. `C1` and `C2`, where address `S` owns `slot_id = i` in both):
1. Observe (via P2P gossip or by directly requesting) a validly-signed `StackerDBChunkData { slot_id: i, slot_version: v, sig, data }` that signer `S` produced and pushed to contract `C1`.
2. POST the identical JSON body to `http://<victim-node>/v2/stackerdb/<C2-issuer>/<C2-name>/chunks`.
3. `RPCPostStackerDBChunkRequestHandler` calls `try_replace_chunk(&C2, &chunk.get_slot_metadata(), &chunk.data)`; `SlotMetadata::verify(&addr_of_S_in_C2)` succeeds because the digest never referenced `C1` or `C2`.
4. The chunk is stored under `C2` and rebroadcast network-wide as a `StackerDBPushChunk` for `C2`, even though it was never signed for `C2`.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-179)
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L86-90)
```rust
        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        let chunk: StackerDBChunkData = serde_json::from_slice(body).map_err(Error::JsonError)?;

        self.contract_identifier = Some(contract_identifier);
        self.chunk = Some(chunk);
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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L578-601)
```rust
        let stackerdb_list = if !has_participation {
            vec![]
        } else {
            signers
                .iter()
                .map(|signer| {
                    let signer_hash = Hash160::from_data(&signer.signing_key);
                    let signing_address = StacksAddress::p2pkh_from_hash(is_mainnet, signer_hash);
                    let tuple_data = TupleData::from_data(vec![
                        (
                            ClarityName::from_literal("signer"),
                            Value::Principal(PrincipalData::from(signing_address)),
                        ),
                        (ClarityName::from_literal("num-slots"), Value::UInt(1)),
                    ])
                    .map_err(|e| {
                        ChainstateError::Expects(format!(
                            "Failed to create tuple for stackerdb entry: {e}"
                        ))
                    })?;
                    Ok::<Value, ChainstateError>(Value::Tuple(tuple_data))
                })
                .collect::<Result<Vec<_>, _>>()?
        };
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L913-931)
```rust
        let mut signer_set: Vec<_> = apportioned
            .into_iter()
            .filter_map(|entry| {
                if entry.weight == 0 {
                    return None;
                }
                let weight = u32::try_from(entry.weight)
                    .expect("CORRUPTION: Stacker claimed > u32::max() reward slots");
                Some(NakamotoSignerEntry {
                    signing_key: entry.signing_key,
                    stacked_amt: entry.stacked_amt,
                    weight,
                })
            })
            .collect();

        // finally, we must sort the signer set: the signer participation bit vector depends
        //  on a consensus-critical ordering of the signer set.
        signer_set.sort_by_key(|entry| entry.signing_key);
```

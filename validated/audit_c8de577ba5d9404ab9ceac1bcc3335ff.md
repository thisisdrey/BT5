### Title
Missing StackerDB contract-id (domain separator) in chunk signature binding allows cross-replica chunk replay - (File: libstackerdb/src/libstackerdb.rs)

### Summary
`SlotMetadata::auth_digest()` computes the message that a StackerDB writer signs over `slot_id`, `slot_version`, and `data_hash` only — it never binds the signature to the target StackerDB contract (`QualifiedContractIdentifier`) or to any chain/network identifier. [1](#0-0)  Because the same principal (signer) is very often assigned the same `slot_id` across multiple, distinct StackerDB replicas — e.g. the `.signers-<set>-<cycle>` contracts, whose slot assignment is deterministic per reward cycle and can persist across cycles when the signer set/order is unchanged [2](#0-1)  — a chunk legitimately signed for one contract can be replayed byte-for-byte into a different contract's identical slot, exactly analogous to the referenced HOPR domain-separator issue where a signature not bound to `chainId`/context could be replayed in another context.

### Finding Description
The signing/verification scheme for StackerDB chunks is:
- `SlotMetadata::auth_digest()` hashes only `slot_id || slot_version || data_hash.0`. [1](#0-0) 
- `SlotMetadata::verify()` recovers the pubkey from that digest and checks it against the expected `StacksAddress` for the slot, again with no reference to which contract this slot belongs to. [3](#0-2) 
- Server-side validation (`PeerNetwork::validate_received_chunk`, used both for sync downloads and unsolicited pushes) looks up the expected signer address by `(smart_contract_id, slot_id)`, but the cryptographic check itself (`slot_metadata.verify(&addr)`) never folds `smart_contract_id` into the signed payload. [4](#0-3) 
- The unauthenticated HTTP write path (`POST /v2/stackerdb/{address}/{contract}/chunks`) takes the `contract_identifier` purely from the URL and independently from the signed chunk body, then calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), ...)`; only the version/slot/signer checks inside that call gate it — none of which depend on `contract_identifier` matching what was signed. [5](#0-4) 

Consequently, if signer `S` legitimately owns `slot_id = k` in StackerDB contract `A` (e.g. `.signers-0-100`) and also owns `slot_id = k` in a different contract `B` (e.g. `.signers-0-101`, a later reward cycle with the same signer ordering), then a chunk `(slot_id=k, slot_version=v, sig, data)` that was validly broadcast/observed for contract `A` is *also* a validly-signed chunk for contract `B`, since the signature payload never encoded which contract it was meant for. Any remote, unprivileged peer that observes the broadcast chunk (StackerDB gossip is public; anyone can query `GET /v2/stackerdb/.../chunks/...` too) can simply re-POST the identical bytes to contract `B`'s HTTP endpoint, or relay it via the p2p unsolicited-push path (`handle_unsolicited_StackerDBPushChunk`), and the version/signer checks will pass as long as `B`'s stored slot version for `k` is `<= v`. [6](#0-5) 

### Impact Explanation
This breaks the equality "chunk authenticated for StackerDB X" vs. "chunk accepted by StackerDB Y" — i.e., data legitimately authored/signed for one context is treated as canonical/valid in a different context, without the signer ever intending or authorizing that. This can be used to plant unauthorized/forged-looking (but not actually forged — replayed) signer messages (e.g. block votes/rejections, `StateMachineUpdate`, mock signatures) into a different reward cycle's `.signers-*` StackerDB, corrupting the replicated state that miners/other signers rely on for coordination (`GlobalStateEvaluator`, vote tallying), which matches the "network-wide propagation of forged data" / "serving non-canonical state as canonical" impact class. It requires no privileged key and no cooperation from the legitimate signer — only observation of a public broadcast chunk and knowledge of the target contract's slot assignment (which is itself queryable on-chain via `stackerdb-get-signer-slots-page`). [7](#0-6) 

### Likelihood Explanation
Moderately likely in practice: slot assignment for `.signers-<set>-<cycle>` StackerDBs is derived deterministically from the sorted signer public keys for that reward set, [8](#0-7)  so a signer whose relative ordering is unchanged between two reward cycles (common when the reward set is stable) keeps the same `slot_id` across those cycles' distinct StackerDB contracts. An attacker only needs to (a) observe one signed chunk (trivially available, since StackerDB chunks are gossiped/queryable) and (b) confirm slot alignment (queryable on-chain), then replay the bytes to the other contract's write endpoint.

### Recommendation
Bind the StackerDB chunk signature to the target contract by including `smart_contract_id` (and ideally a chain/network identifier) inside `SlotMetadata::auth_digest()`, e.g. hash `smart_contract_id || slot_id || slot_version || data_hash`. This requires updating `sign`/`verify` call sites to pass the contract id, and bumping any wire/version compatibility as needed for existing signed chunks.

### Proof of Concept
1. Set up two StackerDB contracts `A` and `B` where address `S` is assigned `slot_id = 0` in both (achievable in `.signers-0-N` and `.signers-0-M` when signer ordering coincides — reproducible in the existing test harness by calling `create_stackerdb` for two `QualifiedContractIdentifier`s with the same `(addr, num_slots)` list, as done in `stackslib/src/net/stackerdb/tests/db.rs`).
2. Sign a chunk for contract `A`: `chunk = StackerDBChunkData::new(0, 1, data); chunk.sign(&S_privkey)`.
3. POST this chunk to `A`'s endpoint — it is accepted (this is the intended flow, verified by `test_request_fail_stale_chunk`-style tests).
4. POST the exact same `StackerDBChunkData` bytes to `B`'s `/v2/stackerdb/{addr}/{contract_B}/chunks` endpoint. Because `SlotMetadata::verify` only checks `(slot_id, slot_version, data_hash)` against the recovered pubkey — never `contract_B` — `try_replace_chunk` in `B` succeeds identically, storing `S`'s contract-`A`-scoped message as valid data in `B`'s replica. [4](#0-3)

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L12-24)
```text
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
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L169-200)
```rust
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
        let http_peer = node.http_peer_addr();

        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
```

**File:** stackslib/src/net/tests/relay/nakamoto.rs (L1224-1247)
```rust
    // --- Test 1: Properly signed chunk should be BUFFERED on the FutureView path ---
    let mut good_chunk_data = StackerDBPushChunkData {
        contract_id: contract_id.clone(),
        rc_consensus_hash: future_consensus_hash.clone(),
        chunk_data: StackerDBChunkData::new(0, 1, vec![1, 2, 3, 4, 5]),
    };
    good_chunk_data.chunk_data.sign(&signer_privk).unwrap();

    let result = peer
        .network
        .handle_unsolicited_StackerDBPushChunk(
            &mut stacks_node.chainstate,
            1,
            &preamble,
            &good_chunk_data,
            false,
        )
        .unwrap();

    assert_eq!(
        result,
        (true, false),
        "chunk with valid signature must be buffered on FutureView path"
    );
```

**File:** libsigner/src/signer_set.rs (L60-77)
```rust
        for (i, entry) in reward_set.iter().enumerate() {
            let signer_id = u32::try_from(i).map_err(|_| Error::SignerCountOverflow)?;
            let signer_public_key = StacksPublicKey::from_slice(entry.signing_key.as_slice())
                .map_err(|e| {
                    Error::BadSignerPublicKey(format!(
                        "Failed to convert signing key to StacksPublicKey: {e}"
                    ))
                })?;

            let stacks_address = StacksAddress::p2pkh(is_mainnet, &signer_public_key);
            signer_addr_to_id.insert(stacks_address.clone(), signer_id);
            signer_id_to_pk.insert(signer_id, signer_public_key.clone());
            signer_pk_to_id.insert(signer_public_key.clone(), signer_id);
            signer_pks.push(signer_public_key);
            signer_id_to_addr.insert(signer_id, stacks_address.clone());
            signer_addr_to_weight.insert(stacks_address.clone(), entry.weight);
            signer_addresses.push(stacks_address);
        }
```

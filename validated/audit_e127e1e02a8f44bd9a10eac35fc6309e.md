### Title
Cross-StackerDB replay of a validly-signed chunk due to missing contract-binding in the chunk signature - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata`/`StackerDBChunkData` signatures only commit to `(slot_id, slot_version, data_hash)`. They never bind the signature to the specific StackerDB `smart_contract_id` being written to. Because several distinct StackerDB replicas (e.g. the per-message-type `.signers-<set>-<message_id>` contracts) assign the *same* signer address to the *same* `slot_id`, an unprivileged peer can take a signer's already-published, validly-signed chunk from one StackerDB contract and replay it verbatim into a different StackerDB contract where that signer also owns the identical slot, causing the node to accept, store, and gossip it as authentic data for a contract the signer never wrote to.

### Finding Description
The signed digest for a StackerDB chunk is computed purely from slot metadata, with no reference to which StackerDB (`QualifiedContractIdentifier`) it belongs to: [1](#0-0) 

`verify()` only recovers the public key from `(slot_id, slot_version, data_hash)` and checks it against the address the caller supplies — it never checks that the address is actually the owner of that slot *in this particular contract*.

The storage/relay-path validation in `PeerNetwork::validate_received_chunk` (used both for `StackerDBGetChunkData` sync responses and for unsolicited `StackerDBPushChunk` gossip) resolves the expected signer strictly by `(smart_contract_id, slot_id)` via `get_slot_signer`, then calls the same contract-agnostic `verify()`: [2](#0-1) 

This is safe *only if* no two StackerDB contracts ever assign the same signer address to the same `slot_id`. That assumption does not hold: the `.signers` boot contract stores one shared `(signer, num-slots)` list per signer set (0 or 1), and every `signers-<set>-<message_id>` contract — one per signer message type (BlockResponse, StateMachineUpdate, BlockPreCommit, mock messages, etc.) — reads that identical list to build its own slot assignment: [3](#0-2) [4](#0-3) 

So for a given reward cycle's signer set, the same signer key occupies the exact same `slot_id` simultaneously across many distinct StackerDB contracts (one per message type). Chunk data (and the corresponding `sig`) is publicly readable from any node (StackerDB GET endpoints require no authentication), so an attacker can fetch a signer's legitimately-signed chunk from contract A (e.g. `signers-0-<StateMachineUpdate id>`) and re-POST the identical `(slot_id, slot_version, sig, data)` tuple to contract B (e.g. `signers-0-<BlockPreCommit id>`), where the same signer owns the same `slot_id`. Since `validate_received_chunk`/`verify()` never check the contract identity, the replayed chunk passes signature verification and is written/stored (subject only to version freshness and `max_writes`), then relayed onward via `StackerDBPushChunk`/`StackerDBChunkInv` gossip to other peers, who accept it under the same contract-agnostic check.

The only mitigation observed is at a higher, node-local layer: `libsigner`'s `StackerDBChunksEvent -> SignerEvent` conversion applies a `signer_message_payload_matches_lane` filter that discards chunks whose payload type-byte doesn't match the target contract's expected message id: [5](#0-4) 

That filter runs only when the event-observer feed is converted into signer-runloop events for the *local* signer process; it is not part of the p2p StackerDB write/replication path itself. Any other consumer that reads StackerDB chunks directly (RPC clients, alternate StackerDB applications, future message types sharing a lane's type-byte, or any node that stores/serves the chunk before this filter runs) sees the forged-context chunk as validly signed and correctly stored for that contract.

### Impact Explanation
This is an unauthenticated write to StackerDB state: an attacker with no private key of their own can force a foreign, differently-purposed message to be accepted and network-wide propagated as if it were legitimately written by a signer to a specific StackerDB contract, because the signature never commits to *which* contract authorized the write. This matches the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact classes.

### Likelihood Explanation
Exploitation requires only: (1) reading a legitimately-signed chunk from a public StackerDB slot (no authentication needed), and (2) knowing that the reward cycle's signer set assigns the same signer address to the same slot index across its sibling message-id contracts (this is a static, discoverable protocol property, not a secret). No node secret or victim key is needed — only replay of already-public data to a different endpoint.

### Recommendation
Include the target `smart_contract_id` (or another unique per-StackerDB identifier) in the digest signed for `SlotMetadata`/`StackerDBChunkData`, so a signature is only valid for the specific StackerDB it was produced for. This requires updating `SlotMetadata::auth_digest` (and the corresponding sign/verify call sites) in `libstackerdb/src/libstackerdb.rs`, plus threading the contract id through `validate_received_chunk` and `StackerDBTx::try_replace_chunk` verification paths in `stackslib/src/net/stackerdb`.

### Proof of Concept
1. Wait for a reward cycle where signer S is assigned `slot_id = k` in both `signers-0-<msgIdA>` and `signers-0-<msgIdB>` (guaranteed, since both derive from the same `stackerdb-signer-slots-0` list).
2. GET the latest chunk at slot `k` from `signers-0-<msgIdA>` (e.g. a `StateMachineUpdate` chunk), recording `(slot_id, slot_version, sig, data)`.
3. POST that identical tuple to `signers-0-<msgIdB>` at slot `k`, using a `slot_version` that is fresh (>= what `signers-0-<msgIdB>` currently has, and within `max_writes`).
4. Observe via `validate_received_chunk`/`SlotMetadata::verify` (`stackslib/src/net/stackerdb/mod.rs:679-697`, `libstackerdb/src/libstackerdb.rs:181-193`) that the chunk is accepted, stored, and relayed to peers under `signers-0-<msgIdB>`, despite S never having signed anything intended for that contract.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
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

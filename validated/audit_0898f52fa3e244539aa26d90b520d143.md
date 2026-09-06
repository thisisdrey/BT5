## Analog Confirmed

### Title
Signer StackerDB Chunk Signature Fails to Bind Message-Type Contract, Enabling Cross-StackerDB Chunk Replay - (File: `libstackerdb/src/libstackerdb.rs`, `stackslib/src/net/stackerdb/db.rs`)

### Summary
The Arbitrum bug's root cause is that a verification step checks *some* fields of an authenticated action (target address, selector) while ignoring other security-critical fields (refund recipients) that are bound to the same action, letting an attacker satisfy the check while diverting a different resource than intended. The same class of gap exists in the StackerDB chunk-signing scheme: the signature only binds `slot_id`, `slot_version`, and `data_hash`, but never binds the specific StackerDB contract (message type) the chunk is destined for. Because a signer occupies the *same* `slot_id` across every `signers-{page}-{message_id}` StackerDB contract for a reward cycle, a validly-signed chunk for one message type can be replayed, verbatim, into a different message-type StackerDB and will pass all server-side checks.

### Finding Description
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`SlotMetadata::verify()` recovers the public key from that digest and checks only that it hashes to the expected principal — it never checks which StackerDB (contract) the metadata is for: [2](#0-1) 

On the storage/validation side, `StackerDBTx::try_replace_chunk` looks up the expected signer for `(smart_contract, slot_id)` and then calls `slot_desc.verify(&slot_validation.signer)` — again, the contract identity is used only to *look up* the expected signer address, and is never itself part of what was signed: [3](#0-2) 

The schema comment even documents that everything except `(slot_id, version, data_hash)` is out of scope of the signature — the `signer` column is explicitly called out as "NOT covered by the signature": [4](#0-3) 

Crucially, for the signer StackerDBs, the slot-to-signer mapping is **shared across all message-type contracts within a signer-set page**. Each `signers-{page}-{message_id}.clar` contract simply defers to the same underlying signer-slot list for that page, regardless of `message_id`: [5](#0-4) [6](#0-5) 

`MessageSlotID` enumerates several distinct message types (`BlockResponse`, `StateMachineUpdate`, `BlockPreCommit`), each mapped to its own contract id (`signers-{page}-{message_id}`) via `stacker_db_contract()`, but a given signer occupies the identical numeric `slot_id` in every one of these contracts, since they all read from the same `stackerdb-get-signer-slots-page`: [7](#0-6) 

Because the signature never encodes the target contract/message type, a chunk that signer `S` legitimately signs and pushes for `slot_id=5, version=V` under the `BlockResponse` contract will also validly recover to `S`'s public key when replayed at `slot_id=5, version=V` against the `StateMachineUpdate` (or `BlockPreCommit`) contract, since these contracts happen to use the same slot list. This is validated identically in the P2P push-chunk path, `validate_received_chunk()`, which likewise checks only size/version/`slot_metadata.verify(&addr)` and never checks which contract/message type the signature was originally produced for: [8](#0-7) 

This mirrors the Arbitrum bug precisely: the authentication check passes (signature recovers correctly, matching the equality "signature was produced by the expected principal"), but a security-relevant binding — *which specific action/contract this signature authorizes* — is silently ignored, letting the payload be diverted/repurposed into an unintended destination.

### Impact Explanation
Any unprivileged network peer that has observed one broadcast/relayed, validly-signed StackerDB chunk from a signer can replay that exact chunk (same bytes, same signature) as a POST to `/v2/stackerdb/{addr}/{contract}/chunks` (or via unsolicited `StackerDBPushChunk`) against a *different* message-type StackerDB contract for the same signer set/page, provided the `slot_id`/`version` line up as fresh for that other contract's slot state. The victim node will accept and store this cross-context chunk as "signed by `S` for `StateMachineUpdate`" even though `S` only ever authorized it as a `BlockResponse` message (or vice versa). This is a network-wide propagation of forged/misattributed data into a different logical StackerDB namespace than the signer intended — an unauthenticated write to state, since it is state that was never actually attested for that specific destination/purpose. Downstream signer/miner logic consuming these StackerDBs by message type could misinterpret replayed bytes (e.g., a `BlockResponse`-shaped payload injected into `StateMachineUpdate`'s slot) as legitimate state for that message type, though the practical blast radius depends on whether downstream deserializers for a given `MessageSlotID` happen to also parse successfully as a payload for a different `MessageSlotID` and whether stale/duplicate data disrupts signer coordination (e.g., replaying stale `BlockResponse` bytes into `BlockPreCommit`'s slot could desynchronize the consensus round for that signer). This qualifies as at least a "network-wide propagation of forged data" issue under the given severity classification.

### Likelihood Explanation
High likelihood of triggerability: no privileged role or secret key is needed by the attacker — they merely observe a legitimately signed, already-broadcast chunk (chunks are gossiped P2P and available via HTTP `GET /v2/stackerdb/.../chunks/{slot}`), then resubmit it verbatim to a sibling `signers-{page}-{other_message_id}` contract. The only preconditions are that (a) the destination slot's expected version is not higher than the replayed chunk's version, and (b) the target contract's slot mapping places the same signer at the same `slot_id` — which is guaranteed by design, since all message-type contracts for a page share the identical signer-slot list.

### Recommendation
Bind the StackerDB contract identity (and ideally the specific `MessageSlotID`/message-type semantics) into the signed digest, e.g., extend `SlotMetadata::auth_digest()` to also hash the `QualifiedContractIdentifier` (or at minimum the `MessageSlotID`/reward-cycle-page) that the chunk is destined for, and thread that value through `sign()`/`verify()`/`try_replace_chunk()`/`validate_received_chunk()`. This closes the gap analogous to enforcing `value == 0` / validating refund addresses in the Arbitrum case — i.e., verifying *all* security-relevant bindings of an authenticated action, not just a subset.

### Proof of Concept
1. During epoch 3.0 with an active signer set, have signer `S` legitimately sign and push a `BlockResponse` chunk at `slot_id=k, version=v` to its `signers-{page}-1` contract (as in the existing test harness pattern, e.g. `StackerDBChunkData::new(slot_id, version, ...); chunk.sign(&signer_private_key)`), as demonstrated in the test helper flow: [9](#0-8) 
2. Fetch that exact chunk (`slot_id`, `slot_version`, `sig`, `data`) via the standard StackerDB GET/inventory APIs (public, unauthenticated read).
3. Resubmit the identical `(slot_id, slot_version, sig, data)` tuple via `POST /v2/stackerdb/{signer_addr}/signers-{page}-2/chunks` (the `StateMachineUpdate` contract) or `signers-{page}-3` (`BlockPreCommit`), provided that contract's stored version for slot `k` is `< v`.
4. Observe that `RPCPostStackerDBChunkRequestHandler` → `try_replace_chunk` accepts and stores the chunk under the new contract, because `slot_desc.verify(&slot_validation.signer)` succeeds (the signature only ever attested to `slot_id`/`version`/`data_hash`, not the destination contract), confirming the cross-contract signature-replay gap.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L160-166)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L61-69)
```rust
        data_hash TEXT NOT NULL,
        -- secp256k1 recoverable signature from the stacker over the above columns
        signature TEXT NOT NULL,

        -- the following is NOT covered by the signature
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
        -- the chunk data itself
        data BLOB NOT NULL,
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

**File:** stacks-node/src/tests/signer/v0/mod.rs (L9082-9090)
```rust
        let mut slot_id = 0;
        while !accepted {
            let mut chunk = StackerDBChunkData::new(slot_id, version, message.serialize_to_vec());
            chunk
                .sign(&signer_private_key)
                .expect("Failed to sign message chunk");
            debug!("Produced a signature: {:?}", chunk.sig);
            let result = session.put_chunk(&chunk).expect("Failed to put chunk");
            accepted = result.accepted;
```

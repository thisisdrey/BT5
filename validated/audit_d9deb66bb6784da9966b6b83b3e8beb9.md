## Analysis

I investigated the StackerDB chunk-authentication path in `libstackerdb/**` and `stackslib/src/net/stackerdb/**` for the bug class hinted by the report ("incomplete signature coverage" — a signature that fails to bind enough context, enabling replay across a boundary it should have covered).

### Finding Description

`SlotMetadata::auth_digest()` computes the signed digest as `hash(slot_id || slot_version || data_hash)` only — it never includes the target StackerDB's smart-contract identifier: [1](#0-0) 

Verification (`SlotMetadata::verify`) only checks that this digest recovers to the expected `principal` address: [2](#0-1) 

The storage-layer gate, `StackerDBTx::try_replace_chunk`, resolves the expected signer *for the target contract* via `get_slot_validation(smart_contract, slot_desc.slot_id)` and then calls `slot_desc.verify(&slot_validation.signer)` — i.e., acceptance depends only on "does this signature recover to the address that owns `slot_id` in *this* contract," not on which contract the chunk was originally produced for: [3](#0-2) 

The same gap exists in the P2P replication path, `StackerDBs::validate_received_chunk`, used both for solicited downloads and for unsolicited `StackerDBPushChunk` messages: it looks up `get_slot_signer(smart_contract_id, data.slot_id)` and calls `slot_metadata.verify(&addr)`, again with no binding to `smart_contract_id` in the signed payload itself: [4](#0-3) 

Critically, in the Nakamoto signer protocol, slot ownership (signer address → slot index) is assigned **per reward-cycle-parity signer set**, and is shared verbatim across *all* per-message-type contracts of that set. The `.signers` boot contract stores one signer-slot list per parity (`stackerdb-signer-slots-0`/`-1`) and every `signers-{0,1}-{message_id}` contract reads from that same shared list: [5](#0-4) [6](#0-5) 

and message-type contracts are enumerated as `signers-{reward_cycle%2}-{message_id}` for `BlockResponse=1`, `StateMachineUpdate=2`, `BlockPreCommit=3`: [7](#0-6) [8](#0-7) 

Consequently, signer `S` who owns `slot_id = N` in `signers-0-1` (BlockResponse) also owns `slot_id = N` in `signers-0-2` (StateMachineUpdate) and `signers-0-3` (BlockPreCommit) for that reward-cycle parity. Because the chunk signature never commits to the contract identifier, a legitimately-signed `StackerDBChunkData` broadcast for one of these contracts is *also* a valid signature for the identical `(slot_id, slot_version, data_hash)` triple in any sibling contract where `S` owns the same slot.

The unsolicited P2P entry point only requires the sending peer to be authenticated at the connection level (handshake completed) — no per-DB privilege is checked before the chunk is handed to `validate_received_chunk`/`try_replace_chunk`: [9](#0-8) [10](#0-9) 

Any unprivileged remote peer who observes a chunk gossiped by `S` for contract A (e.g., via network sniffing/relay observation) can replay the identical `StackerDBChunkData` (same `slot_id`, `slot_version`, `sig`, `data`) into contract B, provided B's stored version at that slot is `<` the replayed `slot_version`. The signature check passes because it is contract-agnostic; the "authenticated for A" vs "stored into B" equality is broken.

### Impact Explanation

This lets an unprivileged network peer store data of one message type (e.g., a `BlockPreCommit` payload) into a slot that downstream signer/monitor code expects to hold a different message type (e.g., `BlockResponse` or `StateMachineUpdate`) for the same signer, as long as version/slot alignment happens to match across the sibling contracts for that reward-cycle parity. This is a cross-context chunk-replay that stores non-canonical/wrong-context data as the canonical latest chunk for a slot the real signer never intended to write in that DB — matching "serving non-canonical state as canonical" and "unauthorized write to state/StackerDB" in the given severity bands. Note: because signature-forgery is not possible (an attacker cannot fabricate *new* data), the confirmed root cause is scoped to **replay of previously-observed, validly-signed chunks across sibling StackerDB contracts that share the same slot-to-signer mapping**, not arbitrary forgery.

### Likelihood Explanation

Requires only: (1) observing one legitimately broadcast, signed chunk (trivial — chunks are gossiped/public), and (2) an authenticated (handshake-only, unprivileged) P2P connection or unauthenticated RPC POST to the target contract's endpoint. No secret key or special role is needed.

### Recommendation

Bind the smart-contract identifier (and ideally the message-type/slot-purpose) into `SlotMetadata::auth_digest()` so a signature is only valid for the specific `(smart_contract_id, slot_id, slot_version, data_hash)` tuple, closing the cross-contract replay window in both `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`) and `validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs`).

### Proof of Concept

1. Reward cycle parity 0 registers signer `S` at `slot_id = 2` in the shared signer-slot list used by `signers-0-1`, `signers-0-2`, and `signers-0-3`.
2. `S` legitimately signs and broadcasts a `BlockPreCommit` chunk to `signers-0-3`: `StackerDBChunkData{slot_id: 2, slot_version: 7, sig, data}`.
3. Attacker (any peer with a completed P2P handshake, or any unauthenticated RPC client) observes this chunk and re-sends the identical `StackerDBChunkData` struct as a `StackerDBPushChunk`/RPC POST targeting `signers-0-1` (BlockResponse) or `signers-0-2` (StateMachineUpdate) instead.
4. `validate_received_chunk`/`try_replace_chunk` for the target contract resolves `S` as the owner of slot 2 there too, `slot_metadata.verify(&S)` succeeds (digest is contract-agnostic), and if the target slot's current version is `< 7`, the chunk is accepted and stored as the latest `BlockResponse`/`StateMachineUpdate` chunk for `S` — even though it is actually `BlockPreCommit`-typed data `S` never intended to place there.

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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L1-43)
```text
(define-data-var last-set-cycle uint u0)
(define-data-var stackerdb-signer-slots-0 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-data-var stackerdb-signer-slots-1 (list 4000 { signer: principal, num-slots: uint }) (list))
(define-map cycle-set-height uint uint)
(define-constant MAX_WRITES u4294967295)
(define-constant CHUNK_SIZE (* u2 u1024 u1024))
(define-constant ERR_NO_SUCH_PAGE u1)
(define-constant ERR_CYCLE_NOT_SET u2)

(define-map cycle-signer-set uint (list 4000 { signer: principal, weight: uint }))

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** libsigner/src/v0/messages.rs (L68-92)
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
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1073)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }

    /// Make the contract ID for a signers DB contract
    pub fn make_signers_db_contract_id(
        reward_cycle: u64,
        message_id: u32,
        mainnet: bool,
    ) -> QualifiedContractIdentifier {
        let name = Self::make_signers_db_name(reward_cycle, message_id);
        boot_code_id(&name, mainnet)
    }
```

**File:** stackslib/src/net/unsolicited.rs (L52-80)
```rust
    #[cfg_attr(test, mutants::skip)]
    /// Check that the sender is authenticated.
    /// Returns Some(remote sender address) if so
    /// Returns None otherwise
    fn check_peer_authenticated(&self, event_id: usize) -> Option<NeighborKey> {
        let Some((remote_neighbor_key, remote_is_authenticated)) = self
            .peers
            .get(&event_id)
            .map(|convo| (convo.to_neighbor_key(), convo.is_authenticated()))
        else {
            test_debug!(
                "{:?}: No such neighbor event={}",
                &self.get_local_peer(),
                event_id
            );
            return None;
        };

        if !remote_is_authenticated {
            // drop -- a correct peer will have authenticated before sending this message
            test_debug!(
                "{:?}: Unauthenticated neighbor {:?}",
                &self.get_local_peer(),
                &remote_neighbor_key
            );
            return None;
        }
        Some(remote_neighbor_key)
    }
```

**File:** stackslib/src/net/unsolicited.rs (L548-577)
```rust
            StacksMessageType::StackerDBPushChunk(ref data) => {
                // N.B. send back a reply if we're calling to buffer, since this would be the first
                // time we're seeing this message (instead of a subsequent time on follow-up
                // processing).
                let (can_buffer, can_store) = self
                    .handle_unsolicited_StackerDBPushChunk(
                        chainstate, event_id, preamble, data, buffer,
                    )
                    .unwrap_or_else(|e| {
                        info!(
                            "{:?}: failed to handle unsolicited {:?} when buffer = {}: {:?}",
                            self.get_local_peer(),
                            payload,
                            buffer,
                            &e
                        );
                        (false, false)
                    });
                if buffer && can_buffer && !can_store {
                    debug!(
                        "{:?}: Buffering {:?} to retry on next sortition",
                        self.get_local_peer(),
                        &payload
                    );
                }
                (can_buffer, can_store)
            }
            _ => (false, true),
        }
    }
```

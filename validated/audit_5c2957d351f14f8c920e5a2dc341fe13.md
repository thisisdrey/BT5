This confirms it: the same signer set/addresses are used across all `.signers-{cycle_mod}-{message_id}` StackerDB contracts for a given reward cycle (`signers.clar` stores one `stackerdb-signer-slots-N` list per signer-set page, shared by every `MessageSlotID` contract via `stackerdb-get-signer-slots-page`), so slot N is assigned to the same signer address in every message-type contract (BlockResponse, StateMachineUpdate, BlockPreCommit) for that cycle.

### Title
StackerDB chunk signature omits the target contract ID, enabling cross-contract chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` (and thus the signature carried in every `StackerDBChunkData`) commits only to `(slot_id, slot_version, data_hash)`. It never binds the signature to the `QualifiedContractIdentifier` of the StackerDB instance the chunk is destined for.

### Finding Description
`SlotMetadata::auth_digest` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 

`verify()` recovers the public key from that same digest and only checks the pubkey hash against the *caller-supplied* `principal: &StacksAddress` — the contract identity is never part of what's authenticated: [2](#0-1) 

The SQL schema comment for `chunks` explicitly documents this: the signer/contract association is "NOT covered by the signature": [3](#0-2) 

Both the write path (`try_replace_chunk`, used by the `/v2/stackerdb/.../chunks` POST RPC) and the gossip validation path (`validate_received_chunk`) only look up "who owns `slot_id` in *this* contract" and then call `slot_metadata.verify(&addr)` — neither checks that the signature was produced for *this* contract: [4](#0-3) [5](#0-4) 

Because the `.signers` boot contract assigns the *same* address to slot `N` across every `signers-{cycle_mod}-{message_id}` StackerDB for a given reward cycle (one shared `stackerdb-signer-slots-N` list read via `stackerdb-get-signer-slots-page` by all message-id contracts), a chunk that signer `S` legitimately signs and posts to slot `N` of, say, the `BlockResponse` contract is *also* a validly-signed chunk for slot `N` of the `StateMachineUpdate` or `BlockPreCommit` contract for that same signer set: [6](#0-5) [7](#0-6) [8](#0-7) 

StackerDB chunks are broadcast/gossiped in the clear (via `StackerDBPushChunk`/HTTP POST responses and inventory sync), so any remote, unauthenticated observer can capture a signer's already-published `StackerDBChunkData` (slot_id, slot_version, sig, data) from one contract (e.g. `signers-1-1`, BlockResponse) and replay the identical bytes as a POST to a *different* contract (e.g. `signers-1-2`, StateMachineUpdate) at the same or any other node, as long as the target contract has that slot at a version ≤ the replayed one. `try_replace_chunk`'s per-contract checks (size, slot lookup, `slot_desc.verify(&slot_validation.signer)`, staleness, max-writes) all pass because the signer address and slot ID match — the contract identity is simply never checked.

### Impact Explanation
This breaks the "authenticated vs. stored" equality that StackerDB relies on: a chunk cryptographically authorized for contract A is accepted and relayed as authoritative content for contract B. This lets an attacker who observes network traffic (no privileged access, no secret key needed) inject a captured BlockResponse-signed blob as a forged StateMachineUpdate (or vice versa) for the same signer, potentially corrupting/confusing signer-set state-machine views or block-response tallies that downstream consumers (miners, other signers) read from StackerDB and treat as canonical. Since accepted chunks are also relayed via `StackerDBPushChunk` to the P2P network, this is network-wide propagation of data attributed to the wrong contract/message type — a High-impact "serving mismatched data as canonical" class issue in this taxonomy (StackerDB replaces "stackerdb-fields" content with attacker-selected valid content from a different context).

### Likelihood Explanation
Moderate: it needs no compromised keys — only capturing an already-broadcast, publicly-observable chunk from one signer contract and replaying it verbatim to another contract's `/v2/stackerdb/.../chunks` endpoint. The main constraint is that the replayed payload's `SignerMessage` type may not deserialize/be semantically meaningful in the target contract's consumer logic, but this project's own comment in `handle_unsolicited_StackerDBPushChunk` acknowledges signature-based protection is the only defense ("protect against big chunks with forged signatures") — cross-contract confusion is not covered by that defense.

### Recommendation
Bind the StackerDB contract identifier (and reward cycle, if applicable) into the signed digest, e.g. include `smart_contract.to_string()` bytes in `SlotMetadata::auth_digest`, and update `SlotMetadata::sign`/`verify` call sites (`try_replace_chunk`, `validate_received_chunk`) to pass and check the contract ID as part of what's authenticated, not just as a lookup key for the signer's address.

### Proof of Concept
1. Observe (via P2P gossip or direct RPC GET) a validly-signed `StackerDBChunkData { slot_id: N, slot_version: V, sig, data }` submitted by signer `S` to contract `signers-<cyc>-1` (BlockResponse).
2. POST the identical bytes to `/v2/stackerdb/<addr>/signers-<cyc>-2/chunks` (StateMachineUpdate) on any node — same `slot_id`/`slot_version`/`sig`/`data`.
3. Because slot `N` in `signers-<cyc>-2` is owned by the same address `S` (per the shared `stackerdb-signer-slots` page), `slot_desc.verify(&slot_validation.signer)` in `try_replace_chunk` succeeds and the chunk is stored and relayed as if it were a genuine StateMachineUpdate chunk from `S`, even though `S` never signed anything intended for that contract.

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

**File:** stackslib/src/net/stackerdb/db.rs (L61-67)
```rust
        data_hash TEXT NOT NULL,
        -- secp256k1 recoverable signature from the stacker over the above columns
        signature TEXT NOT NULL,

        -- the following is NOT covered by the signature
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L15-24)
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
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L40-43)
```text
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
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

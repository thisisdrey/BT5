This confirms the vulnerability. The `try_replace_chunk`/`insert_chunk` path in `stackslib/src/net/stackerdb/db.rs` and `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs` only bind the signature check to `smart_contract` via a separate lookup of `get_slot_validation(smart_contract, slot_id).signer`, but the signature itself (`SlotMetadata::auth_digest`) never includes the `smart_contract_id`. Combined with the fact that sibling StackerDB contracts (`signers-{set}-{message_id}`) for the same signer set share an identical slot→signer mapping, this allows cross-contract chunk replay.

### Title
StackerDB chunk signature omits contract identifier, enabling cross-contract chunk replay - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::auth_digest()` computes the signed digest solely from `slot_id`, `slot_version`, and `data_hash`, with no binding to the StackerDB's `smart_contract_id`. Because the Nakamoto `.signers-{set}-{message_id}` StackerDB contracts for a given signer set/reward cycle all derive their slot→signer mapping from the same underlying page (`stackerdb-get-signer-slots-page`), the same signer address occupies the same `slot_id` across multiple sibling contracts (e.g., `signers-0-1` for `BlockResponse` and `signers-0-2` for `StateMachineUpdate`). A signature that authenticates a chunk for one contract therefore also authenticates the identical `(slot_id, slot_version, data_hash)` tuple for any other contract sharing that slot assignment.

### Finding Description
The signing/verification logic lives in `libstackerdb/src/libstackerdb.rs`: [1](#0-0) 
This digest is verified against a per-`(smart_contract_id, slot_id)` signer address via: [2](#0-1) 
and [3](#0-2) 
Both checks confirm "this signature was produced by the private key controlling address A" and "address A owns this slot in this contract" — but never confirm "this signature was produced *for this contract*". Since the Clarity boot contracts assign slots identically across message-id lanes for the same signer set: [4](#0-3) [5](#0-4) 
a chunk `(slot_id, slot_version, sig, data)` legitimately signed and broadcast by a signer for the `BlockResponse` lane can be captured off the wire (or from the HTTP API) and replayed verbatim against a different lane contract (e.g., `StateMachineUpdate` or `BlockPreCommit`) for the same signer set/reward cycle. Both `validate_received_chunk` (used for both downloaded and unsolicited-pushed chunks) and `try_replace_chunk` (used by the HTTP POST chunk endpoint) will accept it, because they only check the signature against the slot's configured signer address — an equality that holds across contracts, and never check which contract the signature was intended for.

The node running the `.signers-*` StackerDB replicas explicitly subscribes to all message-id contracts for both signer sets simultaneously: [6](#0-5) 
so the replay target is always locally available, and pushed chunks are also gossiped network-wide via `StackerDBPushChunkData`.

### Impact Explanation
This is an unauthenticated/unauthorized write into a StackerDB contract the signer never intended to write to, and the forged chunk propagates network-wide via StackerDB gossip (`StackerDBPushChunkData`) exactly like a legitimate write — matching the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact tier. An attacker can inject an attacker-observed-but-not-attacker-authored payload into the wrong message lane (e.g., inject what was really a `BlockResponse` payload into the `StateMachineUpdate` or `BlockPreCommit` slot), corrupting the state the miner coordinator (`stacks-node/src/nakamoto_node/stackerdb_listener.rs`) and other signers read from that lane, and can also consume/occupy the slot version so the legitimate signer's next real write to that lane is rejected as stale until a higher version is reused.

### Likelihood Explanation
Exploitation requires no private key and no special permission — only the ability to observe one broadcast/gossiped chunk (all StackerDB writes are plaintext on the wire and readable via the public HTTP StackerDB API) and to submit an HTTP POST or unsolicited P2P push to a sibling contract. This is trivially repeatable for every reward cycle/signer set, since the sibling-contract slot mapping is deterministic and public.

### Recommendation
Include the `smart_contract_id` (or at minimum the numeric `message_id`/StackerDB contract identifier) inside `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB contract it was produced for, analogous to including `chainId` in EIP-712-style domain separation. This requires a coordinated protocol/version bump across `libstackerdb`, the signer's `sign`/`verify` call sites, and any consumers relying on the current digest format.

### Proof of Concept
1. Observe (via P2P gossip or `GET /v2/stackerdb/{contract}/chunks`) a validly signed `StackerDBChunkData { slot_id, slot_version, sig, data }` broadcast by signer `S` to `.signers-0-1` (BlockResponse) for reward cycle `R`.
2. Compute `.signers-0-2` (StateMachineUpdate) contract id for the same reward cycle `R` via `NakamotoSigners::make_signers_db_contract_id(R, 2, mainnet)`; confirm `slot_id` maps to the same signer address `S` there too (guaranteed, since both derive from `stackerdb-get-signer-slots-page u0`).
3. Re-submit the identical captured `StackerDBChunkData` bytes to `.signers-0-2`'s chunk endpoint (`POST /v2/stackerdb/{ST000...signers-0-2}/chunks`) or push it unsolicited over P2P.
4. Observe that `validate_received_chunk`/`try_replace_chunk` accept it (since `slot_metadata.verify(&addr)` succeeds against `.signers-0-2`'s slot owner, identical to `.signers-0-1`'s), storing/propagating data that signer `S` never signed for the `StateMachineUpdate` lane.

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

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
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

**File:** stacks-node/src/tests/signer/mod.rs (L1855-1864)
```rust
    for signer_set in 0..2 {
        for message_id in 0..SIGNER_SLOTS_PER_USER {
            let contract_id =
                NakamotoSigners::make_signers_db_contract_id(signer_set, message_id, false);
            if !naka_conf.node.stacker_dbs.contains(&contract_id) {
                debug!("A miner/stacker must subscribe to the {contract_id} stacker db contract. Forcibly subscribing...");
                naka_conf.node.stacker_dbs.push(contract_id);
            }
        }
    }
```

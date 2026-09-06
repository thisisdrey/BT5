This confirms the vulnerability. The mitigation at `libsigner/src/events.rs:568-619` (`signer_message_payload_matches_lane` check) is a **client-side/signer-side filter only**, applied when the signer parses `StackerDBChunksEvent` into typed messages. It does not exist on the node's write-acceptance path.

The root cause: `SlotMetadata::auth_digest()` in `libstackerdb/src/libstackerdb.rs:160-166` only commits to `slot_id`, `slot_version`, and `data_hash` — it never binds the signature to the specific StackerDB contract (`smart_contract`/`message_id`/lane) the chunk is destined for:

```
138:166:libstackerdb/src/libstackerdb.rs (paraphrased range)
fn auth_digest(&self) -> Sha512Trunc256Sum {
    let mut hasher = Sha512_256::new();
    hasher.update(self.slot_id.to_be_bytes());
    hasher.update(self.slot_version.to_be_bytes());
    hasher.update(self.data_hash.0);
    Sha512Trunc256Sum::from_hasher(hasher)
}
```

The node's write path, `StackerDBTx::try_replace_chunk` in `stackslib/src/net/stackerdb/db.rs:400-437`, and the P2P push path, `PeerNetwork::validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs:649-718`, both authenticate a chunk solely by resolving `get_slot_signer(smart_contract, slot_id)` and calling `slot_metadata.verify(&addr)`. Because the domain (contract id / message lane) is never part of what's signed, any signature that verifies for one contract's `(slot_id, addr)` pair verifies identically for *any other* contract where that same address occupies that same `slot_id`.

Per-cycle, the `.signers-{0,1}-{message_id}` contracts (`stackslib/src/chainstate/nakamoto/signer_set.rs:1061-1063`, `stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar`, `signers-1-xxx.clar`) all derive their slot assignment from the exact same underlying page (`stackerdb-get-signer-slots-page`), so the identical signer occupies the identical `slot_id` across `BlockResponse` (message_id=1), `StateMachineUpdate` (message_id=2), and `BlockPreCommit` (message_id=3) contracts for a given signer set/reward cycle. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

### Title
StackerDB signature auth_digest does not bind to the target contract, allowing cross-lane chunk replay across `.signers-{set}-{message_id}` StackerDBs - (File: libstackerdb/src/libstackerdb.rs)

### Summary
An unprivileged remote attacker can take any legitimately-signed `StackerDBChunkData` observed from one StackerDB instance (e.g. a `BlockResponse` chunk from `.signers-0-1`) and replay it verbatim into a *different* StackerDB instance that assigns the same signer address to the same `slot_id` (e.g. `.signers-0-2` `StateMachineUpdate`, or `.signers-0-3` `BlockPreCommit`), without possessing the signer's private key. The node's chunk-store and P2P-push validation logic accepts it as authentic and stores/broadcasts it network-wide.

### Finding Description
`SlotMetadata::auth_digest()` (`libstackerdb/src/libstackerdb.rs:159-166`) computes the signed digest from only `slot_id`, `slot_version`, and `data_hash`. It never includes the `QualifiedContractIdentifier` of the StackerDB the chunk is meant for. `SlotMetadata::sign`/`verify` (lines 168-193) operate purely on this digest.

On the write path, `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-437`) resolves the expected signer via `get_slot_validation(smart_contract, slot_desc.slot_id)` and then calls `slot_desc.verify(&slot_validation.signer)`. Because `verify()` is domain-agnostic, this check succeeds for *any* smart_contract whose slot table assigns the same address to `slot_id`, provided the local slot version is fresh enough (`slot_version > slot_validation.version` and `<= max_writes`, lines 424-436). The identical acceptance logic exists in the P2P push-validation path, `PeerNetwork::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-718`), which is invoked from `handle_unsolicited_StackerDBPushChunk` and from `StackerDBSync`. Upon acceptance via the HTTP POST endpoint (`RPCPostStackerDBChunkRequestHandler::try_handle_request`, `stackslib/src/net/api/poststackerdbchunk.rs:197-323`), the node re-broadcasts the stored chunk via `StacksMessageType::StackerDBPushChunk` — i.e., successful storage triggers network-wide gossip propagation (`relay_message`/`process_stacker_db_chunks`, `stackslib/src/net/relay.rs:2385-2467`).

Slot assignment for `.signers-{0,1}-{message_id}` contracts is derived identically for every message-id lane sharing a signer set: `NakamotoSigners::make_signers_db_contract_id`/`make_signers_db_name` (`stackslib/src/chainstate/nakamoto/signer_set.rs:1060-1073`) name contracts `signers-{reward_cycle%2}-{message_id}`, and each lane's `stackerdb-get-signer-slots` (`stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar`, `signers-1-xxx.clar`) delegates to the *same* underlying page (`stackerdb-get-signer-slots-page`, `stackslib/src/chainstate/stacks/boot/signers.clar:39-43`). Therefore a given signer occupies the same `slot_id` in the `BlockResponse` (id=1), `StateMachineUpdate` (id=2), and `BlockPreCommit` (id=3) contracts for its signer set simultaneously.

An attacker who observes (via GET `/v2/stackerdb/.../chunks/...`, or by sniffing P2P gossip) a legitimately signed chunk from signer X in slot S of lane A can immediately POST that exact `StackerDBChunkData` (unchanged bytes/signature) to lane B's HTTP endpoint (same signer set, same slot S), as long as lane B's currently-stored version for slot S is lower than the replayed chunk's `slot_version`. `try_replace_chunk`'s signer check passes because `verify()` only checks `(slot_id, slot_version, data_hash)` against the address, and that address is the legitimate slot S owner in lane B too. The node stores the wrong-typed payload under signer X's identity in lane B and relays it to the whole network as an authentic `StackerDBPushChunk`.

The only existing mitigation, `signer_message_payload_matches_lane` in `libsigner/src/events.rs:568-595`, is applied purely on the *consuming signer's* event-processing path when converting a `StackerDBChunksEvent` to typed `SignerMessage`s — it filters what a signer instance chooses to interpret, but does nothing to prevent the *node* from accepting, storing, and gossiping the cross-lane forged chunk in the first place.

### Impact Explanation
This is a network-wide propagation of forged/misattributed data (Critical, per the given severity mapping): a value attributable to signer X becomes durably stored and broadcast under a wrong domain (e.g., a `BlockResponse` payload stored and gossiped as a `StateMachineUpdate` chunk, or vice versa), corrupting the on-chain-adjacent StackerDB state that miners and other signers rely on for coordination, without the attacker needing any private key — only knowledge of a public, gossip-observable chunk and any node's open write RPC.

### Likelihood Explanation
High. The attacker needs no special role: any node's `/v2/stackerdb/.../chunks` POST endpoint is open to unauthenticated peers, chunks are readable via a symmetric GET endpoint or by observing P2P broadcast traffic, and slot assignment overlap across lanes within a signer set is guaranteed by the boot contracts' design (not an edge case).

### Recommendation
Bind the signed digest to the target StackerDB's identity: include the `QualifiedContractIdentifier` (or at minimum the `message_id`/lane) inside `SlotMetadata::auth_digest()` so a signature is only valid for its intended contract instance. This is a wire/consensus-adjacent change and requires coordinated signer/node upgrade (versioned signing scheme).

### Proof of Concept
1. Let signer X hold `slot_id = 5` in both `.signers-0-1` (`BlockResponse` lane) and `.signers-0-2` (`StateMachineUpdate` lane) for the current signer set — guaranteed by shared `stackerdb-get-signer-slots-page` in `signers.clar`.
2. Signer X legitimately posts a valid `StackerDBChunkData{slot_id: 5, slot_version: 10, sig, data: <BlockResponse bytes>}` to `.signers-0-1`; it is accepted and broadcast.
3. Attacker (no keys) fetches this exact chunk via `GET /v2/stackerdb/<signers-0-1>/5/10` (or captures it from gossip).
4. Attacker POSTs the identical `StackerDBChunkData` bytes to `/v2/stackerdb/<signers-0-2 issuer>/<signers-0-2>/chunks`.
5. `try_replace_chunk` on the `.signers-0-2` replica resolves the slot-5 signer to X's address (same address as in lane 1), calls `slot_desc.verify(&X)`, which succeeds since the digest never referenced the contract; the stale/foreign version check passes if `.signers-0-2`'s current slot-5 version is `< 10`.
6. The node stores the `BlockResponse`-typed bytes into the `StateMachineUpdate` lane and relays `StackerDBPushChunk` to the whole network as though it were an authentic `StateMachineUpdate` chunk from signer X.

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

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
```rust
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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1063)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
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

**File:** libsigner/src/events.rs (L568-595)
```rust
        } else if event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot() {
            let Some((signer_set, message_id)) =
                get_signers_db_signer_set_message_id(event.contract_id.name.as_str())
            else {
                return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
            };
            // signer-XXX-YYY boot contract
            //
            // NOTE: the payload-type check below uses v0 `SignerMessageTypePrefix` semantics
            // (the mapping in `signer_message_payload_matches_lane` is fixed to v0). Future
            // signer-message versions must extend that mapping, or their chunks will not be
            // recognized here regardless of which `T` is in scope.
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
```

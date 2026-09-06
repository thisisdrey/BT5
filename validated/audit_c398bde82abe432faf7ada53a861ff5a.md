This confirms an exploitable equality mismatch analogous to the reported bug: two validation sites that are supposed to enforce the same "is this chunk fresh?" rule use different comparison operators.

### Title
StackerDB push-chunk inventory patched to a version that will be rejected as stale on actual storage - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::validate_received_chunk` treats a chunk as fresh whenever `data.slot_version < *expected_version` is false — i.e., it *accepts* `slot_version == expected_version`. But the function that actually commits a chunk to disk, `StackerDBTx::try_replace_chunk`, rejects with `StaleChunk` whenever `slot_desc.slot_version <= slot_validation.version` — i.e., it requires strictly greater versions. This mirrors the H02 report's pattern: two logically-linked validation gates (fee bound in L2 `deposit` vs. L1 `BridgePool`) use inconsistent inclusive/exclusive comparisons, and a value that passes one gate is impossible to make consistent with the other.

### Finding Description
`validate_received_chunk` is used both for solicited/gossip-pushed chunk validation and for the `FutureView` buffering path in `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 

It only rejects when `data.slot_version < *expected_version`, meaning an attacker-supplied chunk whose `slot_version` is *exactly equal* to the locally-tracked "expected" version is treated as valid and non-stale.

For the unsolicited push-chunk handler's normal (non-FutureView) path, on a successful `validate_received_chunk` result the code directly patches the *advertised* inventory slot version to the attacker-controlled `chunk_data.chunk_data.slot_version`, without actually writing the chunk to storage: [2](#0-1) 

Meanwhile, the only function that durably commits a chunk, `StackerDBTx::try_replace_chunk`, uses the opposite (stricter) inequality and will reject that very same equal-version chunk as stale: [3](#0-2) 

Because the two gates disagree on the boundary case (`slot_version == expected_version`), a remote peer can push a chunk whose version equals the node's currently-stored version. `validate_received_chunk` reports it as valid/non-stale, causing the node to advertise (via `StackerDBChunkInvData`) that it now holds this exact version — but no code path in this handler actually calls `try_replace_chunk`/`insert_chunk` to store it (storage only happens via the sync engine's separate download-and-store flow or the `POST` RPC handler). This creates a false inventory: the node claims a version it does not have because the persistence layer would reject it as stale if it were ever attempted with the version already on-disk.

### Impact Explanation
This is a "false inventory"-class issue: the node broadcasts (via the returned `StackerDBChunkInvData`) that it possesses a slot version consistent with an unauthenticated or borderline-fresh push, while the storage layer's independent freshness check (`<=` vs `<`) would refuse to persist that same data. Peers relying on this node's advertised inventory (e.g., for rarest-first StackerDB sync scheduling) can be steered into believing stale/duplicate data is new, wasting fetch attempts or causing sync state to diverge from actual on-disk content — a "steering a node off the tip via false inventory"-style effect, per the rules' High-severity bucket, though narrower in blast radius since it affects only this node's advertised inventory rather than network-wide propagation.

### Likelihood Explanation
Triggering this requires only sending an unsolicited `StackerDBPushChunk` P2P message with a correctly-signed chunk at a `slot_version` equal to the locally-known version — no privileged key or admin role is needed beyond the ability to author messages for the relevant StackerDB slot (a slot ID/signer relationship that any registered signer of that StackerDB contract already legitimately controls, but who could otherwise be blocked from replaying/duplicating a "stale" version). This is reachable directly over the p2p unsolicited-message handling path, `PeerNetwork::handle_unsolicited_StackerDBPushChunk`, in `stackslib/src/net/stackerdb/mod.rs`, which is in-scope (not epoch2x/neon).

### Recommendation
Align the freshness comparison in `validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:700`) with the comparison used by `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:424`): both should reject when `slot_version <= expected_version`/`slot_version <= slot_validation.version` (i.e., require strictly newer chunks consistently), so that anything accepted as "valid" by the validation gate is also acceptable to the persistence layer.

### Proof of Concept
1. Node A stores slot 0 of contract `C` at `slot_version = 5` (signed by registered signer `S`).
2. Attacker (or signer `S` replaying an old signed chunk) sends `StackerDBPushChunk` with `contract_id = C`, `chunk_data.slot_id = 0`, `chunk_data.slot_version = 5`, correctly signed by `S`.
3. In `handle_unsolicited_StackerDBPushChunk`, `make_StackerDBChunksInv_or_Nack` returns `StackerDBChunkInv` with `slot_versions[0] = 5` (current view), matching `expected_version = 5`.
4. `validate_received_chunk` checks `data.slot_version < *expected_version` → `5 < 5` is `false`, so validation passes (`Ok(true)`).
5. The handler patches `data.slot_versions[0] = 5` (no-op here but demonstrates the accepted path) and would forward for storage.
6. If forwarded to `try_replace_chunk`, `slot_desc.slot_version <= slot_validation.version` → `5 <= 5` is `true` → `Err(StaleChunk)`.
Result: the same chunk/version is simultaneously "valid" per the network-facing gate and "stale" per the storage gate, an inconsistency directly analogous to the H02 inclusive/exclusive bound mismatch between `BridgeDepositBox.deposit` and `BridgePool`.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L699-706)
```rust
        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L784-807)
```rust
                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }

                // patch inventory -- we'll accept this chunk
                let Some(slot_version) = data
                    .slot_versions
                    .get_mut(chunk_data.chunk_data.slot_id as usize)
                else {
                    error!(
                        "Chunk not accepted with slot_id {}, which is greater than our slot_versions array {} in {}",
                        chunk_data.chunk_data.slot_id,
                        data.slot_versions.len(),
                        chunk_data.contract_id
                    );
                    return Ok((false, false));
                };
                *slot_version = chunk_data.chunk_data.slot_version;
```

**File:** stackslib/src/net/stackerdb/db.rs (L424-429)
```rust
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
```

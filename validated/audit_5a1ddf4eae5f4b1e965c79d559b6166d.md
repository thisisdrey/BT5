### Title
Off-by-one in `validate_received_chunk`'s version check allows equal-version chunk replay to poison in-memory StackerDB inventory - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`StackerDBs::validate_received_chunk` rejects a pushed chunk only when `data.slot_version < *expected_version`, so a chunk whose version is **exactly equal** to the currently tracked version passes validation. The caller, `handle_unsolicited_StackerDBPushChunk`, then unconditionally overwrites the in-memory `StackerDBChunkInvData::slot_versions` entry with this non-newer version, without ever going through the stricter `<=` rule enforced when chunks are actually persisted to the on-disk StackerDB.

### Finding Description
`validate_received_chunk` (stackslib/src/net/stackerdb/mod.rs:649-718) performs several checks (size, slot bound, signer/signature, version, max_writes), but the version freshness check at line 700 is:
```rust
if data.slot_version < *expected_version {
    ...
    return Ok(false);
}
```
This only rejects strictly stale (`<`) versions; an attacker-supplied chunk with `data.slot_version == *expected_version` passes through and the function returns `Ok(true)` [1](#0-0) .

The caller `handle_unsolicited_StackerDBPushChunk` uses this result to directly patch the in-RAM inventory that will be advertised to the network:
```rust
if !self.validate_received_chunk(...)? { return Ok((false, false)); }
...
*slot_version = chunk_data.chunk_data.slot_version;
``` [2](#0-1) 

This patched `slot_version` is the value fed back into a `StackerDBChunkInv` reply sent to peers, representing this node's claim about which chunk version it holds for that slot — it is not re-validated against the on-disk `<=` rule that governs actual chunk persistence (`try_replace_chunk` / storage path in `stackslib/src/net/stackerdb/db.rs`). Because the legitimate slot owner (an "unprivileged" party who legitimately controls their own slot key) can produce a validly-signed chunk carrying the *same* version number as what is already stored but with *different content*, this in-memory bookkeeping can be advanced/repeated for a version that is not strictly newer, decoupling the advertised inventory state from the actual persisted chunk state.

### Impact Explanation
An attacker who legitimately owns a StackerDB slot can repeatedly push chunks with a version equal to the currently expected version but with different payloads. Each such push is accepted by `validate_received_chunk` and causes the node to patch its live `StackerDBChunkInvData` to reflect this replayed/equivocated version, even though the actual on-disk chunk (guarded by the stricter `<=`/strictly-increasing rule) is not updated to match. This causes the node to advertise inventory that does not correspond to what it actually stored, which can mislead peers about which chunk content/version is canonically held — a false-inventory condition that can steer sync partners away from fetching the real update, matching the "steering a node off the tip via false inventory" impact category. The effect is repeatable per push message and requires only that the attacker hold a legitimately-owned slot in the target StackerDB.

### Likelihood Explanation
The precondition is modest: the attacker must be a legitimate signer/owner of at least one slot in a StackerDB (a normal, unprivileged capability for many StackerDB configurations, e.g. signer or miner-coordination DBs), and must be able to reach the node's P2P port to send an unsolicited `StackerDBPushChunk`. No secret, admin role, or other peer's key is required. The message cost is a single small P2P push per attempt, and the behavior is fully repeatable.

### Recommendation
Change the version freshness check in `validate_received_chunk` to require strictly greater version numbers, mirroring the persistence-layer rule:
```rust
if data.slot_version <= *expected_version {
    return Ok(false);
}
```
This ensures the in-memory inventory patched in `handle_unsolicited_StackerDBPushChunk` can never diverge from the invariant enforced at the storage layer.

### Proof of Concept
Rust test in `stackslib/src/net/stackerdb/mod.rs` (or its test module):
1. Set up a `StackerDBs` instance with a configured contract/DB and a slot owned by a test key.
2. Construct `expected_versions = vec![5]` (i.e., slot 0 currently at version 5).
3. Build a `StackerDBChunkData` with `slot_id = 0`, `slot_version = 5` (equal, not greater), valid `data` within `chunk_size`, and sign it with the slot owner's key so `slot_metadata.verify(&addr)` succeeds.
4. Call `stackerdbs.validate_received_chunk(&contract_id, &config, &chunk_data, &expected_versions)`.
5. Assert the call returns `Ok(true)` — proving the equal-version chunk is incorrectly accepted at line 700, in contrast to the strictly-greater rule enforced by chunk persistence.

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

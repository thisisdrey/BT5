### Title
Stale StackerDB chunk inventory advertised before write-frequency/storage is confirmed, causing served vs. committed mismatch - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` optimistically patches and replies with a `StackerDBChunkInv` claiming the new chunk version has been accepted, *before* the chunk is actually durably written to the local StackerDB replica. The real persistence path (`StackerDBTx::try_replace_chunk`) enforces write-frequency and max-write limits that are *not* checked at this point, so the version advertised to peers can diverge permanently from what is actually stored.

### Finding Description
`validate_received_chunk` at `stackslib/src/net/stackerdb/mod.rs:649-718` explicitly documents that it "does not check write frequency, since the caller has different ways of doing this," and only validates size, signer signature, minimum version and max-write count.

`handle_unsolicited_StackerDBPushChunk` (same file, `mod.rs:742-871`) calls `validate_received_chunk` (`mod.rs:785-792`), and upon success **immediately mutates the outgoing inventory** to reflect the new version: [1](#0-0) 
It then wakes the sync state machine and, at `mod.rs:862-870`, signs and sends this patched `StackerDBChunkInv` back to the peer as a reply — all *before* the chunk is actually committed to the on-disk StackerDB via `StackerDBTx::try_replace_chunk` (which happens later, asynchronously, in the relayer path). The function's own doc comment at `mod.rs:731-733` states plainly: "The write frequency is not checked for this chunk," acknowledging that a subsequent real write can still be rejected.

`try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`) is the actual gate that enforces `StaleChunk`, `TooManySlotWrites`, and (via the write-frequency deadline mechanism referenced by `Error::TooFrequentSlotWrites`) throttling that `validate_received_chunk` never evaluates. If that later write fails for any of these reasons, the node has already told its peers (in a signed p2p reply) that it holds the new chunk version — an **equality break between "advertised/served inventory" and "actually committed state."** Peers that trust this inventory will treat the node as having the chunk (e.g., skip re-requesting it, or use it as a source in future sync rounds), while the local replica in fact still holds the old (or no) chunk.

### Impact Explanation
This falls under "serving non-canonical state as canonical" / false-inventory categories in scope: an unauthenticated remote peer can push a chunk that passes the light-weight unsolicited-path checks but is later rejected by the durable-storage gate, yet the victim node has already broadcast a signed inventory claiming possession of that (unstored) version. This can steer peers' StackerDB sync decisions off the true replicated state, and in aggregate, repeated exploitation degrades the eventual-consistency guarantees the whole StackerDB replication protocol depends on (`stackslib/src/net/stackerdb/mod.rs:29-52`).

### Likelihood Explanation
Any peer already permitted to push unsolicited StackerDB chunks (any connected/authenticated p2p neighbor, not requiring the node's private key or an admin role) can trigger this by pushing chunks in a pattern that passes `validate_received_chunk` but hits the write-frequency/backlog limits enforced only inside `try_replace_chunk`. No privileged access or the target's keys are required — only a validly-signed chunk from the legitimate slot signer (or the signer's own client misbehaving/racing) sent at a rate that trips throttling.

### Recommendation
Do not construct/send the optimistic `StackerDBChunkInv` reply from `handle_unsolicited_StackerDBPushChunk` until the chunk has actually been durably committed (i.e., move the inventory patch after a successful `try_replace_chunk`/relayer store, or re-validate against actual on-disk state before replying), or explicitly check write-frequency/backlog conditions before advertising acceptance.

### Proof of Concept
1. A signer/neighbor pushes a validly-signed `StackerDBPushChunkData` with a strictly-increasing `slot_version` at a rate high enough to exceed the local node's configured write-frequency deadline (which is only enforced in `try_replace_chunk`, not in `validate_received_chunk`).
2. `handle_unsolicited_StackerDBPushChunk` runs `validate_received_chunk` (passes, since frequency isn't checked there), patches `slot_versions` in the outbound `StackerDBChunkInv` (`mod.rs:794-807`), and sends this signed reply to the pushing peer (`mod.rs:862-870`).
3. The relayer later attempts the real storage via `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`), which rejects the write due to `TooFrequentSlotWrites`/`TooManySlotWrites`.
4. The node's replica still holds the old chunk/version, but it has already told the network (via the signed inv reply) that it has the new one — the served inventory no longer matches committed state.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L794-807)
```rust
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

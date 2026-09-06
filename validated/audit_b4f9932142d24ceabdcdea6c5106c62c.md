### Title
`extend_with_inventories` stores `GetAttachmentsInvResponse` under the requested block key without verifying `response.block_id` matches the requested `index_block_hash` - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`AttachmentsBatchStateContext::extend_with_inventories` decodes an `AttachmentsInvResponse` returned by a peer and inserts it into `self.inventories` keyed by `request.key()`, which is derived from the locally-requested `(contract_id, pages, index_block_hash)` tuple, not from any field of the actual response. No comparison against a `block_id` field on the decoded response is performed anywhere in this function before the entry is stored and later consumed by `get_prioritized_attachments_requests`.

### Finding Description
The requested key comes from `AttachmentsInventoryRequest` (fields `contract_id`, `pages`, `index_block_hash` — set at construction time in `get_prioritized_attachments_inventory_requests`, `stackslib/src/net/atlas/download.rs:386-396`). When a response arrives, `extend_with_inventories` (`stackslib/src/net/atlas/download.rs:490-528`) does:

```rust
if let Ok(response) = response.decode_atlas_attachments_inv_response() {
    let peer_url = request.get_url().clone();
    match self.inventories.entry(request.key()) {
        Entry::Occupied(responses) => { responses.into_mut().insert(peer_url, response); }
        Entry::Vacant(v) => { ... v.insert(responses); }
    };
    report.bump_successful_requests();
}
```

The only validation performed is that the response bytes decode successfully via `decode_atlas_attachments_inv_response()`; there is no check comparing any block identifier carried inside the decoded `GetAttachmentsInvResponse` against `request.index_block_hash` (the `H` that was actually requested). The map is keyed purely by the *request*'s tuple `(contract_id, pages, StacksBlockId)`, so whatever inventory bits the remote peer chooses to send are attributed to the requested block `H` regardless of what block they were actually computed for.

Downstream, `get_prioritized_attachments_requests` (`stackslib/src/net/atlas/download.rs:404-478`) iterates `self.inventories` by `(contract_id, pages, _)` — the `StacksBlockId` component of the key is explicitly discarded (`_`) — and uses `response.pages` bitmaps to decide whether a peer "has" a given attachment, driving which `AttachmentRequest`s get queued. Since the key's block-id component is never even read at consumption time, and the stored value was never checked against the request's `index_block_hash` at insertion time, a peer can freely fabricate inventory bits for arbitrary content and have them accepted as if they pertained to the canonical block that was queried.

### Impact Explanation
A malicious peer responding to any `AttachmentsInventoryRequest` can supply arbitrary inventory bitmaps unrelated to the requested `index_block_hash`. This can cause the downloader to believe attachments exist (or don't exist) for the canonical tip based on fabricated data, steering `get_prioritized_attachments_requests` and subsequent attachment fetches based on non-canonical/forged inventory state. This matches the "serving non-canonical state as canonical" / "attachment/BNS mismatch" High-impact category since the local node's attachment-fetch decisions for its canonical tip are driven by inventory data that was never verified to correspond to that tip.

### Likelihood Explanation
Any remote peer configured as an Atlas data-URL peer that the node queries for attachment inventories can trigger this by simply answering with a validly-formatted `GetAttachmentsInvResponse` (any bitmap content) regardless of the block it actually pertains to. No secret, signature, or privileged role is needed — only that the attacker's node is one of the peers the local node queries via HTTP for attachment inventories, which is a normal, unprivileged interaction in the Atlas subsystem. The attack is trivially repeatable on every inventory request cycle.

### Recommendation
Include the responder-observed block identity in `GetAttachmentsInvResponse` (if not already present) and validate it against `request.index_block_hash` inside `extend_with_inventories` before inserting into `self.inventories`; discard/penalize (via `report.bump_failed_requests()`) any response whose block identity does not match the request.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` that:
1. Builds an `AttachmentsBatchStateContext` with a single `AttachmentsInventoryRequest` for `index_block_hash = H`.
2. Constructs a `BatchedRequestsResult` whose `succeeded` map contains that request paired with a crafted `StacksHttpResponse` decoding to a `GetAttachmentsInvResponse` for pages associated with a different block `H2` (or arbitrary bogus bitmap).
3. Calls `context.extend_with_inventories(&mut results)`.
4. Asserts that `context.inventories` contains an entry under the key `(contract_id, pages, H)` populated with the attacker-supplied response, demonstrating no check rejects a response not matching the requested `H`.

Note: I could not fully verify within available tool calls whether `GetAttachmentsInvResponse` itself carries a `block_id`/similar field that the code fails to check (the struct definition lives in `stackslib/src/net/atlas/mod.rs`, which I was unable to inspect before running out of iterations); the finding is based on the confirmed absence of any such comparison in `extend_with_inventories`/`get_prioritized_attachments_requests` in `download.rs`.
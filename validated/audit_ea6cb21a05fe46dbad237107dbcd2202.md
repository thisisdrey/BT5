This confirms the vulnerability. The server side `try_handle_request` in `getattachment.rs` looks up the attachment purely by the hash key in `atlasdb.find_attachment`, but any malicious/compromised peer can serve arbitrary `content` for a `GET /v2/attachments/<hash>` since it's just an HTTP responder — an attacker running their own peer/node fully controls the JSON body returned for this endpoint. The client-side `decode_atlas_get_attachment` and `extend_with_attachments` never check `Hash160::from_data(&attachment.content) == request.content_hash`.### Title
Missing content-hash verification in `extend_with_attachments` allows unprivileged peer to inject forged BNS attachment data - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GET /v2/attachments/<hash>` response and inserts `response.attachment` into `self.attachments` without ever comparing `Hash160::from_data(&attachment.content)` against the `request.content_hash` that was originally requested. Because the served attachment is later persisted verbatim via `network.atlasdb.insert_instantiated_attachment(&attachment)` in `AttachmentsDownloader::run`, any peer can return attacker-chosen bytes under a content hash it does not actually match, and that forged content will be accepted, stored, and resolved against the on-chain-committed hash.

### Finding Description
The equality that should hold is `Hash160::from_data(&attachment.content) == request.content_hash`, since `request.content_hash` originates from an on-chain-confirmed `AttachmentInstance` (see `get_prioritized_attachments_requests`, `stackslib/src/net/atlas/download.rs:404-478`, which builds `AttachmentRequest{content_hash: content_hash.clone(), ...}` from the batch's tracked instances).

In `extend_with_attachments`:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
``` [1](#0-0) 

There is no comparison against `request.content_hash` anywhere in this block, or anywhere between `decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:159-165`) and the insertion into `self.attachments`. The `request` variable (which holds `content_hash`) is dropped/ignored — only `response` is used.

The server side (`RPCGetAttachmentRequestHandler::try_handle_request`, `stackslib/src/net/api/getattachment.rs:93-130`) is a normal HTTP responder on an unauthenticated RPC endpoint; an attacker can run their own peer/node and serve arbitrary JSON for any `GET /v2/attachments/<hash>` request — nothing on the client forces the returned `content` to hash to the requested path's hash.

Downstream, `AttachmentsDownloader::run` drains `context.attachments` and unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for each one:
```
for attachment in context.attachments.drain() {
    ...
    network.atlasdb.insert_instantiated_attachment(&attachment)...
    ...
    context.attachments_batch.resolve_attachment(&attachment.hash())
}
``` [2](#0-1) 

`attachment.hash()` is computed from the (attacker-controlled) `content`, so `resolve_attachment` is called with the attacker's self-consistent hash, not the originally-requested `content_hash` — meaning the batch entry that was actually waiting for `content_hash` gets marked resolved with unrelated/forged data as long as any attachment with a hash matching *some* entry lands in the set. Even in the narrower case, the insertion into `context.attachments` (a `HashSet<Attachment>`) and its subsequent DB write happens with zero hash validation at all — the check that "served bytes' hash == requested content_hash" is simply absent from this code path.

### Impact Explanation
An unprivileged peer serving attachment RPC responses can get forged content persisted into a victim node's Atlas attachment database and served onward through the node's own `GET /v2/attachments/<hash>` endpoint and gossip/propagation paths, since `insert_instantiated_attachment` stores it without re-validating the hash-to-content binding at read time either (that check exists only at request dispatch time, via the URL path regex capturing the hash, not upon storage of downloaded content). This lets an attacker have a BNS name's `zonefile`/attachment resolve to attacker-chosen data that no on-chain name operation actually committed to — matching the "High: attachment/BNS mismatch" impact category. This is repeatable per attachment request and requires no special privilege beyond running a peer the victim's node happens to sync from.

### Likelihood Explanation
Preconditions: the attacker's node/peer URL must be included among the victim's set of outbound sync peers with Atlas support (`network.get_outbound_sync_peers()` / `get_data_url`), and it must be selected as a source for the specific `content_hash` (i.e., it must claim in its attachments inventory to have the attachment, or be tried). No RPC secret, StackerDB key, or admin privilege is needed — any node that peers with the victim over the standard Atlas attachment-inventory/download flow can respond to `AttachmentRequest`s. This makes the attack straightforward: run a normal peer, advertise the attachment in inventory pages, and answer the download request with forged content.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, compute `Hash160::from_data(&response.attachment.content)` and compare it to `request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and optionally record the peer as faulty/malicious. This mirrors the existing empty-hash special case handled in `check_attachment_instances` (`stackslib/src/net/atlas/download.rs:241-245`) and ensures `insert_instantiated_attachment` only ever persists content whose hash matches the on-chain-committed `content_hash`.

### Proof of Concept
Rust unit test in `stackslib/src/net/atlas/tests.rs` (or a new test module in `download.rs`):
1. Construct an `AttachmentRequest { content_hash: H, sources, stacks_block_height, canonical_stacks_tip_height }` where `H = Hash160::from_data(b"expected")`.
2. Build a mock `StacksHttpResponse` whose JSON body decodes via `decode_atlas_get_attachment()` to `GetAttachmentResponse { attachment: Attachment { content: b"attacker-controlled bytes".to_vec() } }`, where `Hash160::from_data(b"attacker-controlled bytes") != H`.
3. Construct a `BatchedRequestsResult<AttachmentRequest>` with `succeeded.insert(request, Some(mock_response))`.
4. Call `AttachmentsBatchStateContext::extend_with_attachments(context, &mut results)`.
5. Assert: `context.attachments.iter().any(|a| a.hash() != H)` is `true` (the mismatched attachment was inserted), demonstrating the missing equality check. A stronger assertion drives the whole `AttachmentsDownloader::run` path and checks that `network.atlasdb.insert_instantiated_attachment` persisted an attachment whose hash differs from the `content_hash` on the corresponding `AttachmentInstance`.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L153-169)
```rust
            AttachmentsBatchStateMachine::Done(ref mut context) => {
                for attachment in context.attachments.drain() {
                    let attachments_instances = network
                        .atlasdb
                        .find_all_attachment_instances(&attachment.hash())
                        .map_err(net_error::DBError)?;
                    network
                        .atlasdb
                        .insert_instantiated_attachment(&attachment)
                        .map_err(net_error::DBError)?;
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
                }
```

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
```

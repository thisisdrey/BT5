### Title
Attachment content is never validated against its requested content hash before being stored, causing valid on-chain attachments to be permanently unresolved and unrelated content to be stored as "instantiated" - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GetAttachmentResponse` and inserts `response.attachment` into `self.attachments` without ever checking that `attachment.hash()` equals the `request.content_hash` that was actually requested. Because `AttachmentsDownloader::run` later resolves queued instances and stores the attachment keyed by `attachment.hash()` (the attacker-controlled value), a malicious/faulty source peer can cause a legitimately committed attachment to remain forever unresolved while unrelated attacker-supplied content gets persisted into the AtlasDB as if instantiated.

### Finding Description
The claimed equality is `attachment.hash() == request.content_hash`, which is expected to hold before an attachment response is accepted, but is never enforced.

- `AttachmentRequest::get_most_reliable_source` (stackslib/src/net/atlas/download.rs) selects a source URL for `AttachmentRequest.content_hash` based on reliability reports, not any cryptographic proof — the "most reliable" peer can still be malicious or simply serve wrong data.
- The response handling path is `extend_with_attachments` at stackslib/src/net/atlas/download.rs:530-558. It calls `response.decode_atlas_get_attachment()` and, on success, does:
```
self.attachments.insert(response.attachment);
report.bump_successful_requests();
```
No comparison against `request.content_hash` is performed anywhere in this loop.
- `decode_atlas_get_attachment` (stackslib/src/net/api/getattachment.rs:159-165) just JSON-deserializes the `GetAttachmentResponse` body into an `Attachment` — it performs no hash check either.
- Later, in `AttachmentsDownloader::run` (download.rs:153-169), the accepted (unchecked) attachment is drained from the `HashSet<Attachment>` and looked up/stored keyed by its own (attacker-controlled) hash:
```
for attachment in context.attachments.drain() {
    let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash())?;
    network.atlasdb.insert_instantiated_attachment(&attachment)?;
    ...
    context.attachments_batch.resolve_attachment(&attachment.hash())
}
```
Because the lookup and the "resolve" call use `attachment.hash()` (the hash of the returned bytes) instead of the `content_hash` that was actually requested and that is committed on-chain via the corresponding `AttachmentInstance`, a peer that returns wrong bytes for a request breaks the equality silently: the correct `content_hash` (e.g., `right`) is never marked resolved, and the wrong hash (e.g., `hash(b"wrong")`) gets inserted into the AtlasDB as an "instantiated" attachment with no `AttachmentInstance` ever pointing to it.

No existing guard catches this: `MAX_MESSAGE_LEN`/HTTP length limits, JSON well-formedness checks, and the 404 handling in `BatchedRequestsState::try_proceed` only validate transport/format, not content-hash correctness. `insert_instantiated_attachment` (stackslib/src/net/atlas/db.rs) stores whatever `Attachment` it is given.

### Impact Explanation
- A remote, unprivileged peer that a node syncs Atlas data from (any outbound sync peer offering an `AttachmentsInventoryRequest`/`AttachmentRequest` response) can cause a legitimate, on-chain-committed attachment (e.g., a BNS zone file) to be permanently marked as unresolved/missing on the victim node, because the correct `content_hash` is never resolved via `resolve_attachment`.
- Simultaneously, the AtlasDB `attachments` table accumulates attacker-chosen content marked `was_instantiated = 1`, i.e., data that was never referenced by any real `AttachmentInstance`/name operation, growing unbounded state that "looks" canonical/instantiated but isn't tied to any committed name operation.
- This matches the High-severity category "serving non-canonical state as canonical, ... attachment/BNS mismatch": a node can end up believing it has resolved/queued state inconsistent with what was actually committed on-chain, and legitimate attachments become permanently unavailable to serving RPCs (`/v2/attachments/<hash>` will 404 for the real hash forever).
- Repeatable: every content request cycle for the same missing attachment can be answered again by the same or another unreliable/malicious peer, keeping the attachment perpetually unresolved (subject to `max_attachment_retry_count` before the batch is dropped entirely — after which it is not even retried).

### Likelihood Explanation
- Precondition: the victim node must have `get_outbound_sync_peers()` include the attacker's peer (or the attacker becomes a reliable/available source in `self.peers`), and the attachment must be queued in an `AttachmentsBatch` (normal Atlas gossip/sync operation, no privileged secret needed).
- Attacker cost: trivial — run a Stacks peer with the correct advertised `data_url`, respond to inbound `/v2/attachments/<content_hash>` GET requests with a well-formed `GetAttachmentResponse` JSON body containing arbitrary attachment content. No RPC secret, signing key, or admin role required.
- Fully remotely reachable via the node's RPC/data URL that is already advertised by the peer for Atlas sync, matching the "remote unprivileged peer" threat model exactly.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558), after `decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; if it does not match, treat it as a failed/faulty response (`report.bump_failed_requests()`, optionally mark the peer as faulty) and do not store or resolve anything for that request. Additionally, `AttachmentsDownloader::run` should resolve/lookup instances and store the attachment keyed by the originally-requested `content_hash`, not by the possibly-forged `attachment.hash()`, as defense in depth.

### Proof of Concept
Rust test plan (net test, e.g. in `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AttachmentsBatchStateContext` with a single peer and an `AttachmentsBatch` tracking one missing attachment with `content_hash = Hash160::from_data(b"right")`.
2. Build an `AttachmentRequest` for that `content_hash`, and craft a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains `(request, Some(mock_http_response))` where `mock_http_response` decodes via `decode_atlas_get_attachment` to `GetAttachmentResponse { attachment: Attachment { content: b"wrong".to_vec() } }`.
3. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments` contains `Attachment{content: b"wrong"}` (hash = `Hash160::from_data(b"wrong")`), proving the mismatch was accepted — i.e. assert `context.attachments.iter().next().unwrap().hash() != Hash160::from_data(b"right")`.
4. Feed this context through `AttachmentsBatchStateMachine::Done` handling (the loop in `AttachmentsDownloader::run`, lines 153-169) against a real `AtlasDB`, then assert:
   - `atlasdb.find_attachment(&Hash160::from_data(b"right"))` returns `Ok(None)` (real committed attachment still missing/unresolved), and
   - `atlasdb.find_attachment(&Hash160::from_data(b"wrong"))` returns `Ok(Some(Attachment{content: b"wrong"}))` with `was_instantiated = 1` in the `attachments` table, despite no `AttachmentInstance` ever referencing `hash(b"wrong")`. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** stackslib/src/net/atlas/download.rs (L530-558)
```rust
    pub fn extend_with_attachments(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentRequest>,
    ) -> AttachmentsBatchStateContext {
        for (request, response) in results.succeeded.drain() {
            let report = self
                .peers
                .get_mut(request.get_url())
                .expect("Atlas: unable to retrieve reliability report for peer");

            let response = if let Some(r) = response {
                r
            } else {
                report.bump_failed_requests();
                continue;
            };

            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
        }
        let mut events_ids = results.faulty_peers.keys().copied().collect::<Vec<usize>>();
        self.events_to_deregister.append(&mut events_ids);

        self
    }
```

**File:** stackslib/src/net/api/getattachment.rs (L158-166)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
}
```

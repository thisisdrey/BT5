### Title
Unvalidated attachment content hash allows peer to spoof `AttachmentRequest` responses and permanently block attachment resolution - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any successfully-decoded `GetAttachmentResponse` and inserts `response.attachment` into `self.attachments` without ever checking that `response.attachment.hash()` equals the `content_hash` that was actually requested (`AttachmentRequest.content_hash`). A malicious/lazy peer that is asked for attachment `H` can return `Attachment::empty()` (or any other bytes) instead, which is accepted, "resolves" nothing of value, and the tracked hash `H` in `AttachmentsBatch::attachments_instances` never matches, so the real content is never retrieved.

### Finding Description
`extend_with_attachments` iterates `results.succeeded`, which pairs each `AttachmentRequest` (which carries the specific `content_hash` that was requested) with the HTTP response received from a peer claiming (via its previously-returned inventory) to hold that attachment: [1](#0-0) 

The only validation performed is `response.decode_atlas_get_attachment()` succeeding (i.e., the payload is valid hex/serde-decodable bytes) — there is no comparison against `request.content_hash`. The `request` variable, which holds the expected `content_hash` (see `AttachmentRequest` construction using `content_hash: content_hash.clone()`), is dereferenced only for `request.get_url()`, never for its hash field.

Downstream, in `AttachmentsDownloader::run`, the accepted attachment is committed to the database and matched against tracked instances purely by recomputing the hash of whatever content was accepted: [2](#0-1) 

`attachment.hash()` is `Hash160::from_data(&content)`, so if a peer substitutes `Attachment::empty()`, the computed hash is `Hash160::from_data(&[])`, not `H`. `context.attachments_batch.resolve_attachment(&attachment.hash())` is therefore called with the wrong hash, and the tracked entry for `H` in `attachments_batch.attachments_instances` is never removed, so `has_fully_succeed()` returns `false` for that batch even though a "successful" HTTP 200 response was recorded and consumed a request slot in this retry cycle. The batch is then re-queued (`bump_retry_count()`), and the same flaw can repeat up to `connection_options.max_attachment_retry_count`.

Existing guards that might have prevented this — `decode_atlas_get_attachment()` only validates hex/serde structure of the payload, not content-hash correctness; there is no `MAX_PAYLOAD_LEN`-style equality check tying response content to the requested hash anywhere in this file or in `GetAttachmentResponse`'s (de)serialization in `stackslib/src/net/atlas/mod.rs`.

### Impact Explanation
Any single remote peer that is included in the `sources` map for a given `content_hash` (i.e., any peer that merely claims, via its self-reported inventory bitmap, to have the attachment) can supply substitute/garbage content instead of the real one. This causes the real attachment (BNS name-registration metadata, zone-file data, etc.) associated with hash `H` to remain permanently unresolved as long as that peer (or any peer behaving the same way) keeps answering requests for it, consuming one of the limited `max_inflight_attachments` request slots per retry cycle and delaying/denying resolution of legitimate BNS attachment data — an attachment/BNS state mismatch as called out in the High severity bucket. It does not corrupt already-resolved/canonical data (the bogus content is stored under its own actual hash, not under `H`), so it is a targeted denial/starvation of attachment resolution rather than data corruction of canonical state.

### Likelihood Explanation
Low cost, fully remote, no privileges needed: the attacker only needs to run a normal outbound-reachable peer node, advertise (truthfully or not) that it has the attachment in its `AttachmentsInv` response so it is chosen as a `source`, and then answer the subsequent `GetAttachment` request with arbitrary bytes (e.g., empty body) instead of the correct content. This is trivially repeatable on every retry cycle for any/all attachments the attacker is asked about, up to `max_attachment_retry_count` before the node gives up on that batch.

### Recommendation
In `extend_with_attachments`, after `response.decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; otherwise treat it as a failed/faulty response (`report.bump_failed_requests()`, and optionally flag the peer via `faulty_peers`/reliability report demotion) so a bad peer is deprioritized on subsequent retries instead of silently consuming request slots.

### Proof of Concept
Rust unit test plan (to be placed alongside `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AttachmentsBatch` tracking one instance with `content_hash = H` (`H = Hash160::from_data(b"real-content")`), and an `AttachmentsBatchStateContext` wrapping it with one peer in `peers`.
2. Build a `BatchedRequestsResult<AttachmentRequest>` where `succeeded` maps an `AttachmentRequest{ content_hash: H, .. }` to `Some(StacksHttpResponse)` whose body encodes `GetAttachmentResponse { attachment: Attachment::empty() }` (empty hex string), simulating the malicious peer's reply.
3. Call `context.extend_with_attachments(&mut results)` and assert:
   - `context.attachments` contains `Attachment::empty()` (hash `Hash160::from_data(&[])`), confirming the empty attachment was accepted.
   - `context.attachments` does NOT contain any attachment whose `.hash() == H`.
4. Simulate the `Done` branch of `AttachmentsDownloader::run` (or call `attachments_batch.resolve_attachment(&Hash160::from_data(&[]))` directly) and assert that `attachments_batch.attachments_instances` still contains the entry keyed by `H`, and `attachments_batch.has_fully_succeed()` returns `false` — proving `resolve_attachment` is never invoked with `H` and the real attachment instance remains unresolved after a "successful" HTTP exchange.

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

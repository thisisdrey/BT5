### Title
Attachment content is accepted and stored without verifying it hashes to the requested `content_hash` - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GetAttachmentResponse` and inserts `response.attachment` directly into `self.attachments` without ever checking that `attachment.hash() == request.content_hash`. A malicious peer serving GET `/v2/attachments/{content_hash}` can return arbitrary bytes, which get stored under the attacker's own hash while the originally-requested (BNS-committed) hash is never resolved.

### Finding Description
The equality that must hold is: for a request keyed on `content_hash` (derived from a confirmed name-op), the returned `attachment.content` must satisfy `Hash160::from_data(&attachment.content) == content_hash`. In `stackslib/src/net/atlas/download.rs`, `extend_with_attachments` (lines 530-558) is:

```rust
pub fn extend_with_attachments(
    mut self,
    results: &mut BatchedRequestsResult<AttachmentRequest>,
) -> AttachmentsBatchStateContext {
    for (request, response) in results.succeeded.drain() {
        ...
        if let Ok(response) = response.decode_atlas_get_attachment() {
            self.attachments.insert(response.attachment);
            report.bump_successful_requests();
        } else {
            report.bump_failed_requests();
        }
    }
    ...
}
```

There is no comparison between `request.content_hash` (the hash that was actually requested, tied to the confirmed name-op) and the hash of the attachment content that was returned. `decode_atlas_get_attachment()` only parses the JSON payload into an `Attachment` struct — it performs no hash verification against the request. The `request` value that carried `content_hash` is discarded (`for (request, response) in ...`, `request` unused beyond peer/report lookup), so by the time attachments propagate to the `Done` state and later `context.attachments.drain()` is consumed by the caller (which calls `atlasdb.insert_instantiated_attachment(&attachment)` and `attachments_batch.resolve_attachment(&attachment.hash())`), resolution is keyed strictly on the attacker-controlled `attachment.hash()`, not on the hash that was originally requested and tied to chain state.

Exploit flow: an unprivileged remote peer serving the Atlas attachment-inventory/attachment RPC endpoint responds to a legitimate GET `/v2/attachments/{H1}` request with a `GetAttachmentResponse` whose `attachment.content` hashes to `H2 != H1`. The victim node accepts this into `self.attachments`, stores it in AtlasDB under `H2`, and marks `H2` (not `H1`) resolved. The genuinely committed attachment for `H1` remains unresolved.

### Impact Explanation
This lets a single malicious peer poison a victim's local Atlas attachment cache with data that was never committed by any confirmed name-op, while making the correctly committed attachment permanently unresolvable for that batch (until retry-count exhaustion drops it). This matches "High: attachment/BNS mismatch" — non-canonical content could be treated as resolved/stored state, and canonical BNS attachment resolution for the legitimate hash is denied/blocked. It is repeatable per request since each `AttachmentsBatch` cycle will re-attempt DNS/inventory/download the same way.

### Likelihood Explanation
Preconditions are minimal: attacker just needs to run a peer node that the victim considers an "outbound sync peer" with a reachable data URL, and be selected as a data source for the requested attachment (which follows from being listed as a source with that content in its self-reported attachments inventory). No secret, admin role, or privileged key is needed — this is exactly the "run their own peer and gossip messages" adversary model. Attacker cost is a single crafted HTTP response with fabricated attachment bytes.

### Recommendation
In `extend_with_attachments`, after successfully decoding `response.attachment`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and do not insert the attachment, so a subsequent retry/source can be tried instead.

### Proof of Concept
Rust test in `stackslib::net::atlas::tests`:
1. Build an `AttachmentsBatchStateContext` with an `AttachmentsBatch` containing one instance whose committed hash is `H1`.
2. Construct a `BatchedRequestsResult<AttachmentRequest>` where `succeeded` maps an `AttachmentRequest { content_hash: H1, .. }` to a `Some(GetAttachmentResponse)`-producing `StacksHttpResponse` whose body is `Attachment { content: b"attacker bytes" }` (hash `H2 != H1`).
3. Call `context.extend_with_attachments(&mut results)`.
4. Assert `context.attachments` contains an `Attachment` with `hash() == H2` (attacker's), and that no attachment in `context.attachments` has `hash() == H1`.
5. Feed this context through to where `attachments_batch.resolve_attachment(&attachment.hash())` would be called (as in `run()`), and assert `attachments_batch.has_fully_succeed()` is `false` and the original `H1` instance is never marked resolved. [1](#0-0) [2](#0-1)

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L641-658)
```rust
            AttachmentsBatchStateMachine::DownloadingAttachment((
                attachments_requests,
                context,
            )) => {
                match BatchedRequestsState::try_proceed(
                    attachments_requests,
                    &context.dns_lookups,
                    network,
                    &context.connection_options,
                ) {
                    BatchedRequestsState::Done(ref mut results) => {
                        let context = context.extend_with_attachments(results);
                        AttachmentsBatchStateMachine::Done(context)
                    }
                    state => AttachmentsBatchStateMachine::DownloadingAttachment((state, context)),
                }
            }
            AttachmentsBatchStateMachine::Done(_context) => unreachable!(),
```

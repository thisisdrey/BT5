### Title
Oversized attachment content accepted from a malicious peer without enforcing `AtlasConfig.attachments_max_size` before storage - (File: stackslib/src/net/atlas/download.rs)

### Finding Description
`AttachmentsBatchStateContext::extend_with_attachments` (download.rs:530-558) processes the results of `AttachmentRequest`s sent to remote peers for a `content_hash` that was committed to by a confirmed name operation. For each successful response it calls `response.decode_atlas_get_attachment()` and, if decoding succeeds, unconditionally does `self.attachments.insert(response.attachment)` [1](#0-0)  with no comparison of `response.attachment.content.len()` against `self.atlas_config.attachments_max_size`. The whole function body (lines 530-558) contains no reference to `attachments_max_size` at all, confirming there is no size gate at this stage [2](#0-1) .

A remote peer answering `/v2/attachments/{content_hash}` fully controls the bytes of the HTTP response body. As long as the payload parses successfully via `decode_atlas_get_attachment()` (bounded only by the generic HTTP/message length limits, not by the Atlas-specific `attachments_max_size` configuration value), an oversized `Attachment.content` (larger than the configured `attachments_max_size`, whose minimum is `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) is accepted into the in-memory `self.attachments` set keyed to a legitimate, previously-confirmed `content_hash`.

### Impact Explanation
The oversized attachment, once accepted into the batch context, is subsequently written to the node's Atlas database via the normal instantiation path, consuming disk space disproportionate to the node's configured `attachments_max_size` policy for a single legitimate commitment. This is a bounded-but-uncapped storage amplification per attachment hash under attacker control, tied to a real, confirmed on-chain commitment (so the attack is repeatable each time the node needs to backfill/re-fetch that hash from an untrusted peer, or for any new hash the attacker's peer is asked to serve).

### Likelihood Explanation
Any unprivileged remote peer that a node queries for an attachment (or that offers itself as a source in the attachment inventory exchange) can trigger this by simply serving an oversized but otherwise well-formed HTTP response for a requested `content_hash`. No secret, signature, or privileged role is required — only that the node attempts to download an attachment from that peer, which is normal Atlas protocol behavior.

### Recommendation
In `extend_with_attachments` (download.rs:547-552), after `decode_atlas_get_attachment()` succeeds, explicitly check `response.attachment.content.len() as u32 <= self.atlas_config.attachments_max_size` before calling `self.attachments.insert(...)`; on failure, treat it as `report.bump_failed_requests()` and drop/blacklist the offending peer/response, mirroring the size enforcement already intended by `AtlasConfig.attachments_max_size`.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` that:
1. Builds an `AttachmentsBatchStateContext` with `atlas_config.attachments_max_size` set to `ATTACHMENTS_MAX_SIZE_MIN` (1_048_576).
2. Constructs a `BatchedRequestsResult<AttachmentRequest>` whose single succeeded entry decodes (via a stub/mock `decode_atlas_get_attachment`) to an `Attachment` with `content.len() == attachments_max_size + 1`.
3. Calls `context.extend_with_attachments(&mut results)`.
4. Asserts `context.attachments` does NOT contain the oversized attachment (expected to fail against current code, since `self.attachments.insert(response.attachment)` at download.rs:548 runs unconditionally).

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

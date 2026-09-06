### Title
Attachment content hash never verified after download - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GET /v2/attachments/{content_hash}` response and inserts `response.attachment` into `self.attachments` without ever comparing the returned attachment's actual hash (`Hash160::from_data(&Attachment.content)`) against the `content_hash` that was requested. Any peer listed as a `source` for that hash (which an attacker can become simply by advertising itself in the attachments inventory) can supply arbitrary bytes that get accepted as the resolved attachment for that content hash.

### Finding Description
The relevant code: [1](#0-0) 

```
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

`request.content_hash` (the key used to build the `AttachmentRequest` in `get_prioritized_attachments_requests`, seen at `stackslib/src/net/atlas/download.rs:467-472`) is never compared to any hash derived from `response.attachment.content` before the attachment is inserted into `self.attachments`. The `request` value carrying `content_hash` is available in scope but discarded (`for (request, response) in ...`, only `request.get_url()` is used for reliability bookkeeping). This is a genuine broken equality: the code should assert `Hash160::from_data(&response.attachment.content) == request.content_hash` before storing/committing the attachment, but no such check exists in this function.

An attacker only needs to be selected as a `source` for a given `content_hash` — achievable by gossiping a `/v2/attachments/inv` response with the corresponding inventory bit set to 1 for that page/index, which is unauthenticated inventory data any peer can advertise about itself. Once selected via `AttachmentRequest::get_most_reliable_source`, the attacker's node answers the `GET /v2/attachments/{content_hash}` request with an `Attachment` whose `content` does not hash to `content_hash`. `extend_with_attachments` will accept and insert it unconditionally as long as `decode_atlas_get_attachment()` succeeds (i.e., the response is well-formed, not necessarily correct).

### Impact Explanation
This lets a single malicious/misbehaving peer poison the node's Atlas attachment resolution for content that was legitimately committed on-chain (e.g., a BNS name-update `content_hash` commitment), causing the node to store/serve wrong zonefile bytes as if they were the canonical attachment for that hash. This matches the "attachment/BNS mismatch" High-severity category: the node would treat non-canonical data as resolved/canonical for a real on-chain commitment.

### Likelihood Explanation
Low cost, fully remote, and repeatable: the attacker just needs to run a peer, advertise it has the attachment in its inventory (setting an inv bit to 1) for the target `content_hash`, and respond to the resulting GET request with forged content. No secret, no privileged role, and no special peer reputation is required beyond `get_most_reliable_source` picking that peer (which is straightforward if the attacker is the only — or best-reporting — source, or via Sybil sources).

### Recommendation
In `extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, compute `Hash160::from_data(&response.attachment.content)` (or equivalent `Attachment::hash()`) and compare it to `request.content_hash`. If they don't match, treat it as a failed request (`report.bump_failed_requests()`), and optionally penalize/blacklist the responding peer; only insert into `self.attachments` when the hash matches.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or a new test module) that:
1. Constructs an `AttachmentRequest` with a known `content_hash = Hash160::from_data(b"real content")`.
2. Builds a fake `StacksHttpResponse` for `/v2/attachments/{content_hash}` whose decoded `Attachment.content` is `b"forged content"` (hash mismatch).
3. Populates a `BatchedRequestsResult<AttachmentRequest>` with this `(request, Some(response))` pair in `succeeded`.
4. Calls `AttachmentsBatchStateContext::extend_with_attachments(ctx, &mut results)`.
5. Asserts that `ctx.attachments` now contains an `Attachment` whose `Hash160::from_data(&attachment.content) != content_hash`, proving the mismatch is accepted and stored — the fix should make this assertion fail (mismatched attachment rejected instead of inserted).

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

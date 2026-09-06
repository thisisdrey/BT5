### Title
Attacker-controlled peer can respond to an `AttachmentRequest` with a mismatched attachment, poisoning `AtlasDB` while leaving the true content hash unresolved - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` inserts the `Attachment` returned by a peer into `self.attachments` without verifying that its content hash matches the `content_hash` of the `AttachmentRequest` that solicited it. Since attachments are indexed downstream by `attachment.hash()` rather than by the original request's `content_hash`, a malicious peer can answer a request for hash `H1` with arbitrary bytes hashing to `H2`, causing the real `H1` attachment to remain permanently unresolved while `H2` gets stored in `AtlasDB` unbacked by any confirmed `AttachmentInstance`.

### Finding Description
The equality that should be enforced — `attachment.hash() == request.content_hash` — is never checked. In `extend_with_attachments`: [1](#0-0) 

the loop iterates `results.succeeded.drain()`, which yields `(request, response)` pairs where `request: AttachmentRequest` carries the `content_hash` that was actually requested (as built in `get_prioritized_attachments_requests`, see `content_hash: content_hash.clone()` at line 469). But once `response.decode_atlas_get_attachment()` succeeds, the code does `self.attachments.insert(response.attachment)` unconditionally — it never compares `response.attachment.hash()` to `request.content_hash`. Any remote peer that is queried for an attachment (a normal, unprivileged interaction reachable simply by being selected as a download source in `sources` when its previously-gossiped inventory claims to have the attachment) can return any byte blob it wants; as long as it decodes into a valid `Attachment` structure, it is accepted into the batch's attachment set. Downstream, the batch is resolved and instantiated using the attachment's own hash rather than the hash it was requested for, so the genuine content for `H1` is never fetched (the state machine believes the request outcome was "succeeded") while unrelated attacker-supplied bytes for `H2` get written into `AtlasDB` via `insert_instantiated_attachment`, with no corresponding `AttachmentInstance` ever having committed `H2` on-chain.

### Impact Explanation
This lets any single malicious/misbehaving peer silently prevent resolution of a specific BNS-relevant attachment (denial of service against name registration/zonefile propagation for that name) and pollute the node's `AtlasDB` with attacker-chosen content stored under a hash that no committed `AttachmentInstance` ever pointed to. This matches the "attachment/BNS mismatch" High-severity category: the node's attachment inventory and storage no longer faithfully reflect canonical, committed state, and the true requested content is dropped without any error being surfaced to retry it correctly.

### Likelihood Explanation
The precondition is only that the attacker run a normal Stacks peer node that gossips an inventory claiming to have the missing attachment (a costless, unprivileged action — no secret, no admin role, no privileged position required) so that it gets selected into `sources` in `get_prioritized_attachments_requests`. Once selected, every response to an attachment-fetch HTTP request is trivially forgeable since there is no hash check at consumption time. This is repeatable for every attachment the attacker's peer is asked about, at zero cost beyond running a peer and answering GETs.

### Recommendation
In `extend_with_attachments`, after decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; if it does not match, treat it as a failed request (`report.bump_failed_requests()`) and do not resolve/store the attachment, so the batch state machine retries fetching the correct content instead of silently substituting attacker-supplied data.

### Proof of Concept
Add a test in `stackslib::net::atlas::tests` that:
1. Constructs an `AttachmentsBatchStateContext` with one pending `AttachmentInstance`/content hash `H1`.
2. Builds a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains `(request_with_content_hash_H1, Some(response_with_attachment_content_hashing_to_H2))` (`H1 != H2`).
3. Calls `context.extend_with_attachments(&mut results)`.
4. Asserts that `context.attachments` contains an `Attachment` with hash `H2` (proving unrequested content was accepted) while the original `AttachmentsBatch.attachments_instances` entry for `H1` is still present/unresolved and no failure was recorded for the request that should have failed hash verification — demonstrating the request for `H1` is incorrectly marked successful and `H2` would be written to `AtlasDB` via `insert_instantiated_attachment` without a matching `AttachmentInstance`.

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

### Title
`bump_successful_requests()` fires on successful decode regardless of content-hash correctness, letting a hash-lying peer become the preferred attachment source - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558) calls `report.bump_successful_requests()` solely based on whether `response.decode_atlas_get_attachment()` returns `Ok(...)`, with no comparison of the decoded attachment's content against `request.content_hash`. A peer that serves a well-formed but hash-mismatched attachment is treated identically to an honest peer and is never added to `events_to_deregister` (only `results.faulty_peers` entries are, at line 554-555), so its `ReliabilityReport` only improves over time.

### Finding Description
The relevant code:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
```
`decode_atlas_get_attachment()` only validates that the HTTP body parses into an `Attachment` structure - it performs no comparison to `request.content_hash`. Consequently `bump_successful_requests()` is invoked purely on parse success, not on serving the correct bytes. The `faulty_peers` set populated from `results.faulty_peers` (network/decode failures caught earlier in the request pipeline) is the only mechanism that leads to `events_to_deregister`; a peer that always returns a syntactically valid but content-mismatched attachment never lands in `faulty_peers`, so it is never deregistered and its reliability score is monotonically reinforced on every successful round.

An attacker can alternate between:
1. Returning a valid, hash-mismatched attachment → counted as "success" → `bump_successful_requests()`.
2. Occasionally returning a genuinely malformed/undecodable response → counted as "failure" → `bump_failed_requests()`, but never routed to `faulty_peers`/`events_to_deregister` through this path.

By biasing the ratio toward (1), the attacker's `ReliabilityReport` score trends upward indefinitely, since nothing in this function penalizes serving wrong content as long as it decodes.

### Impact Explanation
Because `ReliabilityReport` scoring does not verify content correctness, a malicious peer can inflate its own reliability score without limit while never being penalized for serving incorrect attachment data. If `AttachmentRequest`'s source-selection ordering (`get_most_reliable_source`/`Ord`) prefers peers with higher reliability scores, this creates a path where a dishonest peer is preferentially selected as the source for future attachment fetches, directly undermining the "prefer trustworthy sources" design intent for BNS attachment resolution. This matches the High-severity category of serving non-canonical/incorrect data preferentially, since the reliability mechanism meant to steer requests away from bad actors instead rewards them.

### Likelihood Explanation
Preconditions are low-cost and fully within an unprivileged remote peer's control: the attacker only needs to be an outbound sync peer already returned by `network.get_outbound_sync_peers()` (any normally connected peer serving Atlas attachment data), and it must respond to attachment-inventory-driven follow-up requests with well-formed but incorrect payloads. No secret, admin role, or privileged state is required - only the ability to answer HTTP GET-attachment requests, which any peer offering an Atlas inventory can do. The attack is fully repeatable across polling cycles since scoring is cumulative and there is no decay or hash-based penalty in this function.

### Recommendation
In `extend_with_attachments`, verify the decoded attachment's content hash against `request.content_hash` (e.g., `Hash160::from_data(&response.attachment.content) == request.content_hash`) before calling `report.bump_successful_requests()`. On mismatch, call `report.bump_failed_requests()` (or a stronger penalty) and add the peer's event id to `events_to_deregister`, treating hash mismatch as equivalent to (or worse than) a faulty/undecodable response.

### Proof of Concept
Rust unit test plan for `stackslib/src/net/atlas/download.rs`:
1. Construct two `AttachmentRequest`s with the same `content_hash`, one entry in `results.succeeded` returning a correctly-hashed `GetAttachmentResponse`, another returning a `GetAttachmentResponse` whose `attachment.content` does not hash to `content_hash` but still decodes successfully via `decode_atlas_get_attachment`.
2. Call `AttachmentsBatchStateContext::extend_with_attachments` for both cases using freshly-initialized `ReliabilityReport`s per peer URL.
3. Assert that after processing, both peers' `ReliabilityReport.request_success` counters are incremented equally (`report_honest.request_success == report_lying.request_success`), demonstrating `bump_successful_requests()` does not distinguish hash-correct from hash-mismatched content.
4. Repeat the loop N times, alternating the lying peer between hash-mismatched-but-decodable and undecodable responses, and confirm the lying peer's event id never appears in `context.events_to_deregister` while its score trends toward or above the honest peer's, per `AttachmentRequest::cmp`/`get_most_reliable_source` ordering. [1](#0-0)

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

### Title
`extend_with_attachments` bumps peer reliability as "successful" without verifying `response.attachment.hash() == request.content_hash` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` calls `report.bump_successful_requests()` on the responding peer's `ReliabilityReport` solely because `decode_atlas_get_attachment()` parsed the HTTP body successfully, without ever comparing the returned `Attachment`'s hash to the `content_hash` that was originally requested via `AttachmentRequest`. This lets a malicious inventory-selected peer serve arbitrary, non-matching attachment content and still be credited as "reliable," poisoning future peer selection.

### Finding Description
The claimed equality — "a bumped-successful reliability report == a peer that delivered the requested `content_hash`" — is broken in the code.

`get_prioritized_attachments_requests` builds an `AttachmentRequest` with a specific `content_hash` and a set of candidate peer `sources` [1](#0-0) . When the batched request framework dispatches these and collects a response, `extend_with_attachments` processes the result:

```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
``` [2](#0-1) 

The `request` variable (which carries `request.content_hash`, the originally requested hash) is in scope in this same loop (`for (request, response) in results.succeeded.drain()` [3](#0-2) ), but it is never used to validate `response.attachment.hash()` against `request.content_hash` before crediting the peer. The only gating condition is that `decode_atlas_get_attachment()` succeeds (i.e., the HTTP body parses into a well-formed `Attachment` structure) — this says nothing about whether the attachment's content matches the hash that was requested.

Downstream, in `AttachmentsDownloader::run`, mismatched attachments are effectively silently dropped from resolution (since `find_all_attachment_instances(&attachment.hash())` won't find any pending instance for an unrelated hash) [4](#0-3) , but this happens *after* the reliability report has already been mutated and persisted back into `self.reliability_reports` [5](#0-4) . There is no rollback of the bump when the delivered content turns out not to match the requested content-hash.

**Attacker's exact message:** A remote peer that is included as a source in `get_prioritized_attachments_requests` (i.e., a peer whose previously-served `GetAttachmentsInvResponse` claimed to have the requested attachment in its inventory) can, upon receiving the `AttachmentRequest` for hash `H`, respond with HTTP 200 and any well-formed attachment payload whose content hashes to `H' != H`. `decode_atlas_get_attachment()` only validates that the body decodes into a `GetAttachmentResponse` structure; it performs no hash equality check against the request.

**Why existing guards don't catch this:** The only checks in the polling loop are HTTP status code (`!= 404`) and successful decode of the JSON/HTTP body [6](#0-5) . Neither of these validates the semantic content-hash equality that the reliability scoring implicitly assumes.

### Impact Explanation
The `ReliabilityReport` for a URL is persisted in `AttachmentsDownloader.reliability_reports` and is fed back into subsequent batches via `get_prioritized_attachments_requests`'s `sources` map, which is the input to `AttachmentRequest::get_most_reliable_source()` for future peer selection. An attacker-controlled peer can therefore cheaply and repeatedly (once per attachment-batch retry cycle) inflate its own reliability score by responding to genuine `AttachmentRequest`s with bogus (non-matching) attachment payloads, since every such response still counts as a "successful request." This steers the node's future attachment-fetch preference toward this malicious peer, increasing the frequency with which the node attempts to resolve attachments from an unreliable/malicious source, sustaining BNS resolution mismatches or degraded attachment availability — matching the "attachment/BNS mismatch" High-impact category. No forged attachment is stored as canonical (mismatched attachments fail to resolve any pending instance), so the damage is confined to reliability-score corruption and degraded service quality rather than acceptance of forged canonical state.

### Likelihood Explanation
Low barrier to entry: the attacker only needs to run an ordinary outbound-reachable Stacks peer that (a) previously advertised (truthfully or falsely) having the attachment in its `GetAttachmentsInv` response so it gets selected as a source, and (b) responds to the follow-up `GetAttachment` request with a 200 status and a decodable but mismatched `Attachment`. No secrets, signatures, or privileged roles are required — this is exactly the "unprivileged remote peer" threat model. The action is repeatable indefinitely for every attachment batch retry.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, compare `response.attachment.hash()` to `request.content_hash` before calling `report.bump_successful_requests()`; if they differ, call `report.bump_failed_requests()` (and optionally treat the peer as faulty/blocklist-worthy) instead of storing the attachment.

### Proof of Concept
Rust net test plan (e.g., added to `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AttachmentsBatchStateContext` with one peer URL and an empty `ReliabilityReport`.
2. Build an `AttachmentRequest` with `content_hash = H` (e.g., `Hash160::from_data(b"expected")`).
3. Construct a `BatchedRequestsResult` whose `succeeded` map contains `(request, Some(http_response))`, where `http_response` decodes via `decode_atlas_get_attachment()` into a `GetAttachmentResponse { attachment: Attachment::new(b"different-content".to_vec()) }`, i.e., `attachment.hash() != H`.
4. Call `context.extend_with_attachments(&mut results)`.
5. Assert that `context.peers.get(&peer_url).unwrap().total_requests_sent == 1` and `total_requests_success == 1` (i.e., `bump_successful_requests` was invoked) even though `response.attachment.hash() != request.content_hash`, demonstrating the broken equality and the reliability-poisoning primitive.

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

**File:** stackslib/src/net/atlas/download.rs (L182-185)
```rust
                // Update reliability reports
                for (peer_url, report) in context.peers.drain() {
                    self.reliability_reports.insert(peer_url, report);
                }
```

**File:** stackslib/src/net/atlas/download.rs (L466-474)
```rust
                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
```

**File:** stackslib/src/net/atlas/download.rs (L534-534)
```rust
        for (request, response) in results.succeeded.drain() {
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

**File:** stackslib/src/net/atlas/download.rs (L899-910)
```rust
                                    Some(response) => {
                                        let peer_url = request.get_url().clone();
                                        if response.preamble().status_code == 404 {
                                            state.faulty_peers.insert(event_id, peer_url);
                                            continue;
                                        }
                                        debug!(
                                            "Atlas: Request {} (event_id: {}) received HTTP 200",
                                            request, event_id
                                        );
                                        state.succeeded.insert(request, Some(response));
                                    }
```

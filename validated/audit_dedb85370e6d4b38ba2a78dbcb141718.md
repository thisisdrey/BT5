I found and confirmed the vulnerability. `extend_with_attachments` in `stackslib/src/net/atlas/download.rs` decodes the HTTP response and calls `self.attachments.insert(response.attachment)` with no comparison of `Attachment::hash()` against `request.content_hash` at all.

### Title
Attachment content hash never verified against requested `content_hash` before being accepted and stored - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When the `AttachmentsBatchStateMachine` downloads an attachment via `GET /v2/attachments/<hash>`, `AttachmentsBatchStateContext::extend_with_attachments` decodes the response and inserts `response.attachment` into `self.attachments` without ever checking that `Attachment::hash()` of the returned bytes matches the `content_hash` that was actually requested. Any peer selected as a source for the request can therefore return arbitrary attachment bytes for a given hash, and the node will accept and persist them as if they were the correct data.

### Finding Description
The download flow is: `AttachmentsDownloader::run` → `AttachmentsBatchStateMachine::try_proceed` (state `DownloadingAttachment`) → `BatchedRequestsState::try_proceed` issues the HTTP GET and stores successful responses in `results.succeeded` keyed by `AttachmentRequest` [1](#0-0)  → `context.extend_with_attachments(results)` is called on completion [2](#0-1) .

Inside `extend_with_attachments`, for each succeeded `(request, response)` pair, the code decodes the response via `response.decode_atlas_get_attachment()` and does `self.attachments.insert(response.attachment)` — it never reads `request.content_hash` or compares it to `response.attachment.hash()`: [3](#0-2) .

The attachment is later drained in `AttachmentsDownloader::run`'s `Done` branch, where it is looked up by `attachment.hash()` (the hash of the possibly-forged bytes, not the originally requested hash) via `find_all_attachment_instances`, and unconditionally written to the database with `network.atlasdb.insert_instantiated_attachment(&attachment)` [4](#0-3) . Because `find_all_attachment_instances` is keyed by the attacker-controlled `attachment.hash()`, if the attacker serves bytes whose hash happens to equal some *other* pending `AttachmentInstance.content_hash` that this node is also trying to resolve, that unrelated pending instance gets resolved with mismatched-context data pushed into `resolved_attachments`. Even absent a hash collision with another pending instance, the fundamental broken invariant is that nothing in this path enforces "bytes returned for request X hash to X.content_hash" before insertion into `self.attachments`, which contradicts the design intent of `AttachmentRequest.content_hash` (the entire point of requesting by content-addressed hash).

`AttachmentRequest::get_most_reliable_source` (used to pick which peer to query) governs peer selection but performs no data validation — it only selects among sources previously reported to have the item in their inventory (self-reported by peers, another attacker-controllable signal), and does not correct for a malicious peer forging inventory + response as the sole/most-reliable source for that hash.

### Impact Explanation
An attacker peer, once selected as the (only, or most-reliable) source for a given `content_hash`, can return attachment bytes of its choosing for that GET request. Since no hash check gates the acceptance path, this can pollute `AtlasDB` with instantiated attachment data whose hash-to-instance binding was never cryptographically verified at the point of insertion in this function. This matches the "attachment/BNS mismatch" High-impact category: a node's Atlas subsystem can end up serving or associating attachment content that doesn't match what the confirmed on-chain name/attachment operation expects, since the guard that should reject mismatched content prior to `insert_instantiated_attachment` does not exist in `extend_with_attachments`.

### Likelihood Explanation
Preconditions are modest and match the "unprivileged remote peer" threat model: the node must have an outstanding `AttachmentInstance` it's trying to resolve, and the attacker must be an outbound sync peer with a registered data URL — both achievable by simply running a normal Stacks node/peer and being selected in the normal p2p peer set, no secrets or privileged roles required. The attack is repeatable on every batch resolution cycle for as long as the attacker remains a selected source.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after `response.decode_atlas_get_attachment()` succeeds, compute `Attachment::hash()` (or `Hash160`) of `response.attachment.content` and compare it to `request.content_hash`; only call `self.attachments.insert(...)` and `report.bump_successful_requests()` if they match, otherwise treat it as a failed/faulty response (`report.bump_failed_requests()`, and optionally mark peer as faulty/deregister).

### Proof of Concept
Rust test plan in `stackslib/src/net/atlas/download.rs` (or a new test module):
1. Construct an `AttachmentRequest` with a known `content_hash = H1` and a `sources` map containing a fake peer URL.
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains `(request, Some(fake_response))`, where `fake_response` is a `StacksHttpResponse` wrapping a `GetAttachmentResponse { attachment: Attachment { content: b"forged bytes".to_vec() } }` such that `Attachment::hash()` of `"forged bytes"` != `H1`.
3. Call `AttachmentsBatchStateContext::extend_with_attachments(context, &mut results)`.
4. Assert that `context.attachments` does NOT contain an attachment whose hash differs from `H1` — currently this assertion fails because `context.attachments.insert(response.attachment)` unconditionally inserts the forged attachment regardless of hash mismatch, demonstrating the missing verification at [5](#0-4) .

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

**File:** stackslib/src/net/atlas/download.rs (L651-654)
```rust
                    BatchedRequestsState::Done(ref mut results) => {
                        let context = context.extend_with_attachments(results);
                        AttachmentsBatchStateMachine::Done(context)
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

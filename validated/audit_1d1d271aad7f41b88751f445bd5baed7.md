### Title
Unvalidated attachment content is persisted regardless of hash match or on-chain commitment - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` inserts whatever `Attachment` content a peer returns for an `AttachmentRequest` into `context.attachments` without ever checking that `response.attachment.hash() == request.content_hash`. When the state machine reaches `Done`, every attachment in that set is persisted via `insert_instantiated_attachment` unconditionally, even when `find_all_attachment_instances(&attachment.hash())` returns empty, meaning the stored content is not tied to any on-chain `AttachmentInstance`/name operation.

### Finding Description
The download pipeline builds an `AttachmentRequest` for a specific `content_hash` that came from a queued, on-chain-derived `AttachmentInstance` (via `track_attachment`/`get_prioritized_attachments_requests`). However, when the HTTP response comes back, `extend_with_attachments` only decodes it and stores the body verbatim: [1](#0-0) 
There is no comparison of the decoded attachment's own hash against the `content_hash` that was originally requested — the peer's `Attachment` bytes are trusted as-is and inserted into `self.attachments` (a `HashSet<Attachment>` keyed by the content itself, not by the request it satisfied).

Then, in the `Done` state of `AttachmentsDownloader::run`, every attachment drained from that set is unconditionally written to disk: [2](#0-1) 
`find_all_attachment_instances(&attachment.hash())` is looked up first, but its result is only used to build `resolved_attachments` for pairing — it is never used as a gate before calling `insert_instantiated_attachment`. So whether or not any real `AttachmentInstance` (i.e., any confirmed name operation) references `attachment.hash()`, the row is stored with `was_instantiated=1` in `AtlasDB`.

Because the hash actually stored is `attachment.hash()` — the hash of whatever bytes the malicious peer chose to send — and not the originally-requested `content_hash`, a malicious outbound-sync peer can respond to any `AttachmentRequest` with arbitrary attacker-chosen bytes. Those bytes get permanently persisted as a "validated" attachment with no corresponding `AttachmentInstance` row, i.e., data that was never authorized by any confirmed on-chain name operation.

### Impact Explanation
A remote, unprivileged peer that a node has selected via `network.get_outbound_sync_peers()` for Atlas sync can cause the node to persist arbitrary, attacker-chosen byte blobs (up to the attachment size limits) into its local attachments store with no cross-check against any real commitment. Repeating this across many `AttachmentsBatch`/`AttachmentInstance` combinations lets the attacker grow the node's attachment storage unboundedly with content that never corresponds to any confirmed on-chain BNS/name operation — an unauthenticated write of unbounded, uncommitted state, matching the Critical "unauthenticated/unauthorized write to state" category.

### Likelihood Explanation
Preconditions are modest for a public node: the attacker only needs to be one of the node's outbound sync peers advertising a reachable Atlas HTTP endpoint (`get_data_url`), and must have at least one legitimate `AttachmentInstance`/`content_hash` queued for the batch to trigger a request (this comes from ordinary chain activity, not attacker privilege). Once a request is issued, the attacker fully controls the HTTP response body served for `GetAttachment`, and the check that would normally bind response content to the requested hash is absent. This is repeatable indefinitely per batch/attachment index, at low attacker cost (no signature, no secret, no special peer state beyond normal peer participation).

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard/penalize (via `report.bump_failed_requests()`) any response whose content hash does not match what was requested. Additionally, in the `Done` handler in `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `find_all_attachment_instances` returns at least one matching, on-chain-derived instance (or otherwise gate persistence on a positive hash match), so unmatched/forged content is dropped rather than stored.

### Proof of Concept
1. Construct an `AttachmentsBatchStateContext` with one queued `AttachmentInstance` whose `content_hash = H` (simulating a real on-chain commitment).
2. Drive the state machine to `DownloadingAttachment`, and simulate a peer HTTP response for the `AttachmentRequest{content_hash: H}` whose `GetAttachmentResponse.attachment` contains different bytes so that `attachment.hash() = H' != H`.
3. Call `extend_with_attachments` and observe `context.attachments` now contains the attacker's attachment keyed by `H'`, with no request/response hash-match check performed.
4. Drive to `Done` and invoke the code path in `AttachmentsDownloader::run` (`stackslib/src/net/atlas/download.rs:152-169`); assert `network.atlasdb.find_all_attachment_instances(&H')` returns empty, yet after `insert_instantiated_attachment` is invoked, querying the `attachments` table shows a row for `H'` with `was_instantiated=1` and no corresponding `attachment_instances` row — confirming persisted, unauthenticated content with no consensus commitment.

*(Note: the exact SQL/schema behavior of `AtlasDB::insert_instantiated_attachment` and `find_all_attachment_instances` in `stackslib/src/net/atlas/db.rs` could not be directly re-verified in this session due to tool-call limits; the control-flow gap identified above is based on the directly-read code in `download.rs`.)*

### Citations

**File:** stackslib/src/net/atlas/download.rs (L152-169)
```rust
        match progress {
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

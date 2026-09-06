### Title
Attachment download resolves batch entries by the downloaded content's hash instead of the originally requested `content_hash`, letting a malicious peer permanently stall resolution of a legitimately committed attachment - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateMachine`/`AttachmentsDownloader::run` never checks that a downloaded `Attachment`'s own hash matches the `content_hash` that was actually requested in the corresponding `AttachmentRequest`. A malicious source peer can answer any `AttachmentRequest{content_hash: H1}` with arbitrary attachment bytes `b2` (hashing to `H2 != H1`), which get accepted, stored, and used to resolve the wrong (or no) batch entry, leaving the legitimately on-chain-committed attachment `H1` permanently unresolved.

### Finding Description
The client builds requests keyed by the on-chain-referenced `content_hash` (`AttachmentRequest.content_hash`, set from `AttachmentInstance.content_hash`) in `get_prioritized_attachments_requests` [1](#0-0) . When a response arrives, `extend_with_attachments` decodes the HTTP body via `decode_atlas_get_attachment()` and blindly inserts `response.attachment` into `self.attachments: HashSet<Attachment>` — there is no comparison against `request.content_hash` at all [2](#0-1) .

Later, in `AttachmentsDownloader::run`, for every attachment collected this way, the code computes `attachment.hash()` from the *received bytes* and uses that (not the original requested hash) to look up instances, persist the attachment, and resolve the batch entry:
```
for attachment in context.attachments.drain() {
    let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash())?;
    network.atlasdb.insert_instantiated_attachment(&attachment)?;
    ...
    context.attachments_batch.resolve_attachment(&attachment.hash())
}
``` [3](#0-2) 

Because `resolve_attachment` is called with `attachment.hash()` rather than the `content_hash` that was actually solicited, a malicious/faulty peer that is selected as a `source` for an `AttachmentRequest` (peer inclusion only requires the peer to self-report the attachment in its own `GetAttachmentsInvResponse` inventory — an unauthenticated, self-asserted signal, see `sources.insert(peer_url.clone(), report.clone())` at [4](#0-3) ) can return unrelated bytes `b2` whose hash `H2 = Hash160::from_data(b2) != H1`. The client:
- stores `b2` in the atlas DB via `insert_instantiated_attachment`, keyed by its own (self-consistent) hash `H2`,
- calls `resolve_attachment(&H2)`, which — since no tracked batch entry has content hash `H2` — does not mark the real `H1` entry as resolved,
- leaves the `H1` entry pending; the batch is judged not `has_fully_succeed()`, gets `bump_retry_count()`ed and re-queued, eventually dropped once `max_attachment_retry_count` is exceeded, at which point the real, on-chain-committed attachment for `H1` is abandoned and treated as unresolved/absent even though it was never legitimately unavailable.

No code path checks `attachment.hash() == request.content_hash` (or equivalently `== H1`) before doing these downstream, peer-triggered state changes.

### Impact Explanation
A remote, unprivileged peer that is merely selected as a download source (by lying in its own attachment inventory response) can, with a single crafted HTTP response, cause the node to treat a real, on-chain-committed attachment as unresolved indefinitely, eventually dropping the retry batch entirely. This is an "attachment/BNS mismatch" condition: the node's local view of attachment availability for `H1` diverges from what was actually committed on-chain, without any real inability to fetch it — purely due to peer-supplied bogus data. This is repeatable per-batch/per-attachment and can be done by any peer that answers the inventory/attachment RPC.

### Likelihood Explanation
Preconditions are low-cost for an attacker: run/operate a peer the victim node treats as an outbound sync peer, answer `GetAttachmentsInv` requests claiming to have the target page/bit set (self-asserted, unauthenticated), then answer the resulting `AttachmentRequest` with arbitrary bytes. No secrets, no privileged role, and no local access are required — this is exactly the "any remote party who can connect to a node's P2P port and send arbitrary bytes / run their own peer" attacker model. The attack is repeatable for every attachment resolution attempt.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard/treat as a failed request (bump `report.bump_failed_requests()`, possibly mark peer faulty) otherwise. Additionally/alternatively, in `AttachmentsDownloader::run`, resolve batch entries using the originally requested hash associated with each downloaded attachment rather than re-deriving it from the downloaded bytes, so a hash mismatch is detected and rejected rather than silently accepted.

### Proof of Concept
1. Construct an `AttachmentsBatch` containing one tracked instance with `content_hash = H1` for some `contract_id`/`index_block_hash`.
2. Drive the state machine to `DownloadingAttachment`, producing an `AttachmentRequest{content_hash: H1, sources: {malicious_peer_url: report}}` via `get_prioritized_attachments_requests`.
3. Simulate the malicious peer's HTTP response: build a `StacksHttpResponse` whose JSON body is `GetAttachmentResponse{ attachment: Attachment{ content: b2 } }` where `Hash160::from_data(b2) = H2 != H1`.
4. Feed this into `BatchedRequestsState`/`extend_with_attachments` so it lands in `results.succeeded`, then call `AttachmentsBatchStateMachine::try_proceed` to reach `Done`.
5. Call `AttachmentsDownloader::run` to completion and assert:
   - `network.atlasdb.find_attachment(&H1)` returns `None` (the real attachment is never marked resolved),
   - `network.atlasdb.find_attachment(&H2)` returns `Some(b2)` (bogus data got stored),
   - the batch's entry for `H1` is still present/pending in `context.attachments_batch` (or the batch is re-queued for retry rather than completed), demonstrating the missing equality check at [5](#0-4) .

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

**File:** stackslib/src/net/atlas/download.rs (L454-459)
```rust
                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
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

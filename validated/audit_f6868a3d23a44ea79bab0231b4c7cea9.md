### Title
`AttachmentsDownloader::run` inserts unauthenticated attachment content into `AtlasDB` without verifying it against any committed `content_hash` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When an `AttachmentRequest` completes, `extend_with_attachments` accepts any successfully-decoded `GetAttachmentsInvResponse`-derived attachment from a remote peer without checking that the attachment's actual hash matches the `content_hash` that was requested. The `Done` branch of `AttachmentsBatchStateMachine` in `AttachmentsDownloader::run` then unconditionally calls `insert_instantiated_attachment` for every attachment received, even when `find_all_attachment_instances(&attachment.hash())` returns no matching on-chain-committed instance.

### Finding Description
The broken equality is: **`attachment.hash() == attachment_instance.content_hash` (the on-chain committed hash) is never checked before the attachment bytes are persisted with `was_instantiated=1`.**

Trace:
1. A batch of `AttachmentRequest`s is built from on-chain `AttachmentInstance.content_hash` values [1](#0-0) .
2. When responses come back, `extend_with_attachments` only checks that `response.decode_atlas_get_attachment()` succeeds (i.e. the bytes parse as valid JSON/attachment framing) and then blindly inserts `response.attachment` into `context.attachments: HashSet<Attachment>` - there is no comparison between the decoded attachment's hash and `request.content_hash`: [2](#0-1) 
3. In the `Done` branch of `run`, the code iterates `context.attachments.drain()`, looks up `find_all_attachment_instances(&attachment.hash())` (computed from the attacker-supplied bytes, not the originally requested hash), and then **unconditionally** calls `insert_instantiated_attachment(&attachment)` regardless of whether any matching instance was found: [3](#0-2) 

Because any outbound peer a node happens to sync with can answer an `AttachmentRequest` with arbitrary bytes that merely satisfy the wire framing of `decode_atlas_get_attachment`, and because the returned attachment is keyed/hashed off its own content rather than the requested `content_hash`, the attacker can inject attachment blobs that have zero corresponding rows in `attachment_instances` (i.e., no name/BNS operation ever committed to that hash on-chain). These blobs are stored as `was_instantiated=1` in `AtlasDB.attachments`.

### Impact Explanation
This lets any remote peer that a node syncs Atlas data from cause the node to persist attacker-chosen content in `AtlasDB.attachments`, permanently marked as "instantiated," with no corresponding canonical on-chain commitment (`attachment_instances` row). This is a mismatch between what is stored/served as validated attachment content and what was actually committed on-chain, matching the "attachment/BNS mismatch" High-severity category. It also allows repeated, low-cost storage filling of the attachments table tied to a fake correlation with consensus state (since the code path is invoked every time the state machine reaches `Done`, i.e., repeatable for every distinct garbage payload the attacker sends).

### Likelihood Explanation
- Precondition: the attacker only needs to be selected as one of the node's outbound Atlas sync peers (`network.get_outbound_sync_peers()`), which requires no privileged role, secret, or admin access - it's satisfied by any peer that legitimately connects and is discovered via the normal peer-graph/Atlas inventory exchange.
- The attacker responds to a normal `GET /attachments/<hash>`-style request with any payload that decodes successfully via `decode_atlas_get_attachment` (i.e., valid JSON matching the response schema) but with unrelated attachment bytes.
- Cost is a single crafted HTTP response per attempt; the vulnerability is repeatable for as many distinct garbage attachments as the attacker wants to send across separate `AttachmentRequest` cycles.

### Recommendation
In `extend_with_attachments` (or immediately before insertion in the `Done` branch of `run`), verify `attachment.hash() == request.content_hash` before accepting the response, and drop/penalize (bump failed requests / mark peer faulty) any response whose attachment hash does not match the requested `content_hash`. Only call `insert_instantiated_attachment` when the computed hash matches at least one entry from `find_all_attachment_instances`, mirroring the semantics implied by the comment/structure of the `Done` branch.

### Proof of Concept
Rust test plan (net test, no new file dependencies beyond existing `atlas` test scaffolding):
1. Set up an `AtlasDB` and register one legitimate `AttachmentInstance` with `content_hash = H` (an on-chain-committed hash) but no corresponding `attachments` row (i.e., unresolved).
2. Spin up a mock peer/HTTP responder for `AttachmentRequest` that, regardless of the requested `content_hash`, returns a `GetAttachmentResponse`-shaped payload wrapping arbitrary/random bytes (attachment content whose hash `!= H`).
3. Drive `AttachmentsDownloader::run` through its state machine (`Initialized -> DNSLookup -> DownloadingAttachmentsInv -> DownloadingAttachment -> Done`) against this mock peer.
4. Assert that `network.atlasdb.find_attachment(&random_bytes.hash())` returns `Some(...)` (i.e., the garbage content was stored with `was_instantiated=1`) even though `network.atlasdb.find_all_attachment_instances(&random_bytes.hash())` returns an empty vector - proving storage of content with zero matching `attachment_instances` rows, exactly at the assertion point corresponding to [4](#0-3) .

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

**File:** stackslib/src/net/atlas/download.rs (L404-478)
```rust
    pub fn get_prioritized_attachments_requests(&self) -> BinaryHeap<AttachmentRequest> {
        let mut queue = BinaryHeap::new();
        let mut enqueued = HashSet::new();
        for ((contract_id, pages, _), peers_responses) in self.inventories.iter() {
            let missing_attachments = match self
                .attachments_batch
                .attachments_instances
                .get(contract_id)
            {
                None => continue,
                Some(missing_attachments) => missing_attachments,
            };
            // Note: we're getting missing_attachments (attachment_id: content_hash)
            for (attachment_index, content_hash) in missing_attachments.iter() {
                let page_index = attachment_index / AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;
                // Since there's a limit in the number of pages that a node can request,
                // we can potentially have multiple inventory request at once.
                if !pages.contains(&page_index) {
                    continue;
                }

                if enqueued.contains(content_hash) {
                    debug!("Atlas: {} already enqueued", content_hash);
                    continue;
                }

                let mut sources = HashMap::new();
                let position_in_page =
                    attachment_index % AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;

                for (peer_url, response) in peers_responses.iter() {
                    // Considering the response, look for the page with the index
                    // we're looking for.
                    let search_page = response.pages.iter().find(|page| page.index == page_index);

                    let has_attachment = search_page
                        .and_then(|search_page| {
                            search_page.inventory.get(position_in_page as usize)
                        })
                        .map(|result| *result == 1)
                        .unwrap_or(false);

                    if !has_attachment {
                        debug!(
                            "Atlas: peer does not have attachment ({}, {}) in its inventory {:?}",
                            page_index, position_in_page, response.pages
                        );
                        continue;
                    }

                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
                }

                if sources.is_empty() {
                    warn!("Atlas: could not find a peer including attachment in its inventory");
                    continue;
                }

                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
            }
        }
        queue
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

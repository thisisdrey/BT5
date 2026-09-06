Based on the code I've traced, this vulnerability is real.

### Title
Attachment content is stored keyed by attacker-controlled self-hash without validating against the solicited `content_hash`, enabling attachment DB pollution with uncommitted data - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GET /v2/attachments/:hash` response and inserts the returned `Attachment` bytes into `context.attachments` with no check that `attachment.hash()` equals the `content_hash` that was actually requested (`AttachmentRequest::content_hash`). `AttachmentsDownloader::run` then unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)`, persisting the attacker-chosen bytes into the `attachments` table keyed by the self-computed hash of the returned content.

### Finding Description
The claimed equality — stored-attachment-hash == a-name-operation-committed `content_hash` — is broken. The request is built in `AttachmentsBatchStateContext::get_prioritized_attachments_requests` (`stackslib/src/net/atlas/download.rs:404-478`), which creates an `AttachmentRequest{ content_hash, .. }` derived from a confirmed `AttachmentInstance.content_hash`. The HTTP request itself, however, is issued by URL path only (`GET /v2/attachments/{content_hash}` in `stackslib/src/net/api/getattachment.rs:147-155`), and the response is decoded purely from JSON body bytes via `decode_atlas_get_attachment` / `GetAttachmentResponse::deserialize` (`stackslib/src/net/atlas/mod.rs:69-77`, `stackslib/src/net/api/getattachment.rs:158-165`) — there is no comparison anywhere between `Hash160::from_sha256(&bytes)` (i.e. `attachment.hash()`) and the `AttachmentRequest.content_hash` that solicited it.

In `extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`), the decoded `response.attachment` is inserted into `self.attachments: HashSet<Attachment>` with no hash equality check against `request.content_hash`. In `AttachmentsDownloader::run` (`stackslib/src/net/atlas/download.rs:152-169`), for each such attachment: `network.atlasdb.find_all_attachment_instances(&attachment.hash())` is called (using the self-computed hash, not the original request's content_hash), and regardless of whether any instances are found, `network.atlasdb.insert_instantiated_attachment(&attachment)` is called unconditionally, persisting the attacker's bytes keyed by their own hash.

A malicious peer that is selected as an outbound sync peer (any remote peer offering an Atlas data URL and appearing in `network.get_outbound_sync_peers()` inventory can be picked) can, upon receiving a legitimate `AttachmentRequest` for `content_hash = H1`, respond with **arbitrary unrelated bytes** whose hash is `H2 != H1`. The node computes `H2` from the received bytes, finds zero matching `AttachmentInstance` records for `H2` (since none was ever confirmed on-chain with `content_hash = H2`), but still stores the attachment under `H2` in the `attachments` table via `insert_instantiated_attachment`.

### Impact Explanation
This lets a remote, unprivileged peer that a node is syncing Atlas data from cause the node to persist arbitrary attacker-chosen blobs (up to the max attachment content size) in its local `attachments` table with no corresponding `AttachmentInstance`/BNS name-operation commitment ever having referenced that hash. This is a storage-pollution / attachment-BNS-mismatch primitive: the node's attachment store now contains records whose hash was never committed to via any confirmed name operation, breaking the intended invariant that every stored attachment corresponds to a consensus-committed `content_hash`. Repeated across many distinct attachment requests, this allows unbounded growth of the `attachments` table (bounded per-record by `ATTACHMENTS_MAX_SIZE`), matching the "attachment/BNS mismatch" High-impact category.

### Likelihood Explanation
Preconditions: the node must be actively running Atlas sync (non-default off in some configs, but on by default when name attachments are enabled) and must have selected the malicious node as an outbound sync peer with the `AttachmentInstance` batch already pending (i.e., the node is asking for *some* attachment). No secret, signature, or privileged role is required — any peer offering a `data_url` and returned in `get_outbound_sync_peers`/inventory results can serve the poisoned response over its RPC port. Attack is trivially repeatable per attachment request cycle and costs the attacker nothing beyond running a normal Stacks node/peer.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or immediately when decoding the response), verify that `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; drop/penalize (bump `report.bump_failed_requests()`) any response that fails this check instead of accepting it.

### Proof of Concept
```rust
// stackslib/src/net/atlas/tests.rs (new test)
#[test]
fn test_extend_with_attachments_no_hash_check() {
    // 1. Seed AtlasDB with an AttachmentInstance whose content_hash = H1
    //    (via atlas_db.insert_initial_attachment_instance / queued_attachments setup).
    // 2. Construct an AttachmentRequest{ content_hash: H1, .. }.
    // 3. Craft a StacksHttpResponse body containing GetAttachmentResponse{ attachment: Attachment::new(b"unrelated bytes") }
    //    whose hash() == H2, H2 != H1.
    // 4. Call AttachmentsBatchStateContext::extend_with_attachments with a
    //    BatchedRequestsResult{ succeeded: {request(H1) -> Some(response_with_H2_bytes)}, .. }.
    // 5. Drive AttachmentsDownloader::run to completion (Done state) so that
    //    network.atlasdb.insert_instantiated_attachment(&attachment) is invoked.
    // 6. Assert atlas_db.find_attachment(&H2).unwrap().is_some() == true,
    //    while atlas_db.find_all_attachment_instances(&H2).unwrap().is_empty() == true
    //    (no AttachmentInstance ever referenced H2), proving unsolicited persistence.
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L69-77)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
}
```

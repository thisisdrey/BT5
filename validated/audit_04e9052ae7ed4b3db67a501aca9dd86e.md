### Title
Remote peer can force unbounded-size `Attachment` content to be written to `AtlasDB` without any `attachments_max_size` check - ([File: stackslib/src/net/atlas/download.rs], [File: stackslib/src/net/atlas/mod.rs])

### Summary
`GetAttachmentResponse::deserialize` (mod.rs:69-77) hex-decodes the response body into `Attachment.content: Vec<u8>` with no length check, and `AtlasConfig.attachments_max_size` is never consulted anywhere along the download path. `AttachmentsBatchStateContext::extend_with_attachments` (download.rs:530-558) and `AttachmentsDownloader::run` (download.rs:152-169) unconditionally call `network.atlasdb.insert_instantiated_attachment(&attachment)` for whatever content a peer returns, storing it into the `attachments.content BLOB` column.

### Finding Description
`GetAttachmentResponse::deserialize` only performs hex-decoding of the payload string; it never compares `bytes.len()` against `AtlasConfig.attachments_max_size` or `ATTACHMENTS_MAX_SIZE_MIN` [1](#0-0) . `StacksHttpResponse::decode_atlas_get_attachment` in getattachment.rs simply parses the JSON body into `GetAttachmentResponse` via `serde_json::from_value`, again with no size gate [2](#0-1) .

In `download.rs`, `AttachmentsBatchStateContext::extend_with_attachments` takes the decoded response and inserts `response.attachment` directly into `self.attachments` (a `HashSet<Attachment>`) with no length check against `attachments_max_size`, and also without verifying that `response.attachment.hash()` matches the originally-requested `AttachmentRequest.content_hash` [3](#0-2) . Then, in `AttachmentsDownloader::run`, once the state machine reaches `Done`, every attachment collected is unconditionally passed to `network.atlasdb.insert_instantiated_attachment(&attachment)` [4](#0-3) . There is no call site anywhere in this path that compares the attachment's content length to `AtlasConfig.attachments_max_size` before this DB write.

The attacker's exact message: become (or already be) an outbound-sync peer that the node queries for attachment inventories/attachments (an unprivileged, remotely reachable role — no secret or admin access required), advertise having the requested attachment in a `GetAttachmentsInvResponse`, and then, when queried via `GetAttachment`, return a `GetAttachmentResponse` JSON body whose hex-encoded `content` decodes to an arbitrarily large `Vec<u8>` (larger than `attachments_max_size`, e.g. tens/hundreds of MB, bounded only by whatever generic HTTP body-size limits exist elsewhere in `net/http`, which are unrelated to the Atlas-specific cap this question is about). This oversized `Attachment` is inserted into `AtlasDB` unconditionally.

### Impact Explanation
This allows a single unprivileged remote peer to cause the node to write an oversized blob into the `attachments` table on every successful `GetAttachment` round it wins, inflating on-disk storage without the `attachments_max_size` cap enforced by the code path — an unauthenticated write of unbounded attacker-controlled data into persistent node state. Because `extend_with_attachments` also never checks the returned attachment's hash against the requested `content_hash`, the write is not even guaranteed to correspond to the item that was actually requested. Repeated across many attachment requests/retries, this is a repeatable per-message storage-inflation vector from a single peer response.

### Likelihood Explanation
Preconditions: the attacker must be selected as (or become) one of the node's outbound sync peers used by `AttachmentsDownloader`, and must serve a positive inventory entry for at least one pending attachment instance so that it is queried directly (`get_prioritized_attachments_requests` / `AttachmentRequest`) [5](#0-4) . Both preconditions are attainable by any peer with an outbound connection to the target node advertising itself as a data-URL peer, requiring no secret, no privileged role, and no local access. The attack requires only one crafted HTTP response per attachment request and is fully repeatable.

### Recommendation
Enforce `attachments_max_size` (and, separately, verify the SHA/Hash160 of the decoded content matches the requested `content_hash`) at the earliest possible point: either inside `GetAttachmentResponse::deserialize`/`decode_atlas_get_attachment`, or immediately in `extend_with_attachments` before inserting into `self.attachments`, and again as a defensive check immediately before `insert_instantiated_attachment` in `AttachmentsDownloader::run`. Reject/drop the response and penalize the peer's reliability report on violation instead of storing the oversized/mismatched content.

### Proof of Concept
```rust
// stackslib/src/net/atlas/tests.rs (conceptual addition)
#[test]
fn test_oversized_attachment_not_size_checked_before_insert() {
    use crate::net::atlas::{GetAttachmentResponse, Attachment, AtlasConfig};

    let cfg = AtlasConfig::new(false); // attachments_max_size = ATTACHMENTS_MAX_SIZE_MIN (1_048_576)
    let oversized = vec![0u8; cfg.attachments_max_size as usize + 1];

    // Simulate what deserialize does: hex-encode then decode, mirroring the wire format
    let hex = stacks_common::util::hash::to_hex(&oversized);
    let json = format!("\"{}\"", hex);
    let resp: GetAttachmentResponse = serde_json::from_str(&json).unwrap();

    // FAILS: no error/rejection occurs even though content exceeds attachments_max_size
    assert!(
        resp.attachment.content.len() as u32 > cfg.attachments_max_size,
        "expected oversized content to be rejected, but GetAttachmentResponse::deserialize accepted it"
    );
    // Following this, download.rs::extend_with_attachments/run would insert this
    // Attachment via AtlasDB::insert_instantiated_attachment with no size gate.
}
```
This demonstrates that `GetAttachmentResponse::deserialize` (mod.rs:69-77) and the surrounding `decode_atlas_get_attachment`/`extend_with_attachments`/`run` path in download.rs never reject content exceeding `attachments_max_size` before it reaches `insert_instantiated_attachment`.

### Citations

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

**File:** stackslib/src/net/api/getattachment.rs (L159-165)
```rust
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

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

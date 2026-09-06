### Title
Missing content-hash verification on Atlas `GetAttachment` responses lets a single malicious peer starve unrelated attachment requests via `HashSet<Attachment>` dedup - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` inserts whatever `Attachment` bytes a peer returns for a `GetAttachment` request directly into a `HashSet<Attachment>` without ever checking that `Hash160(attachment.content) == request.content_hash` [1](#0-0) . A malicious peer that answers every distinct `AttachmentRequest` in a batch with identical bytes causes all of those responses to collapse into a single `HashSet` entry, and the resolution step then pairs that one blob only to `AttachmentInstance`s whose real, distinct `content_hash` happens to equal that blob's hash, leaving every other legitimately-requested attachment instance unresolved.

### Finding Description
The equality the code implicitly relies on is: for every succeeded `AttachmentRequest` with a given `content_hash`, the corresponding entry placed into `context.attachments` actually has `attachment.hash() == content_hash`. Nothing enforces this.

- `RPCGetAttachmentRequestHandler`'s client-side `decode_atlas_get_attachment` merely JSON-parses the response body into `GetAttachmentResponse` with no hash check against the request that was sent [2](#0-1) .
- `extend_with_attachments` then does `self.attachments.insert(response.attachment)` for every succeeded request, regardless of which `content_hash` it corresponds to [3](#0-2) .
- Because `attachments` is a `HashSet<Attachment>` (dedup by full struct equality, not by which hash was requested) [4](#0-3) , if a malicious peer returns byte-identical content for every one of the N distinct `content_hash` values it was asked to serve in a batch, all N inserts collapse to a single `HashSet` entry.
- In the `Done` state, resolution iterates `context.attachments.drain()` and calls `find_all_attachment_instances(&attachment.hash())`, pairing that single blob to every `AttachmentInstance` whose committed `content_hash` equals that blob's real hash, then calls `context.attachments_batch.resolve_attachment(&attachment.hash())` for only that one hash [5](#0-4) .
- Every other distinct, genuinely different `content_hash` in the batch is never resolved from this response set; `attachments_batch.has_fully_succeed()` remains false, so the batch is re-enqueued and retried, and eventually dropped once `retry_count >= max_attachment_retry_count` [6](#0-5) .

For the attacker to be selected as a source for every one of those distinct `content_hash` requests, it need only falsely claim (in its `GetAttachmentsInv` response, likewise unauthenticated/unverified content) to have every attachment in its inventory; `get_prioritized_attachments_requests` will then add it as a candidate source for all pending `AttachmentRequest`s in the batch [7](#0-6) .

### Impact Explanation
A single malicious outbound-sync peer can cause an entire attachment batch (all genuinely different, correctly committed `content_hash` values requested in that round) to fail resolution, because only the one hash matching the attacker's fixed served bytes is accepted; the rest are retried until the retry budget is exhausted and the batch is dropped. This is a data-availability/denial-of-resolution defect for BNS attachment data — matching the "attachment/BNS mismatch" High-impact category (state that no canonical commitment actually maps to is what silently "wins" resolution, and other canonical BNS records fail to resolve).

### Likelihood Explanation
The attacker only needs to be a normal outbound peer the victim node syncs Atlas data with (no privileged role, no secret, no admin access) and to respond to the standard `/v2/attachments-inv` and `/v2/attachments/:hash` RPC endpoints with attacker-controlled bytes — both are ordinary unauthenticated P2P/RPC interactions. The attack is fully repeatable each batch cycle and costs the attacker nothing beyond running a node that answers these two endpoints.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after decoding a `GetAttachmentResponse`, verify `response.attachment.hash() == request.content_hash` before inserting; discard/penalize (bump `report.bump_failed_requests()` and consider marking the peer faulty) if it doesn't match, and key the accepted-attachments collection by `content_hash` (e.g., `HashMap<Hash160, Attachment>`) rather than a bare `HashSet<Attachment>`, so responses can never satisfy a request they weren't actually asked to fill.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/tests.rs` (or a new test module) plan:
1. Build an `AttachmentsBatchStateContext` with an `AttachmentsBatch` containing 5 `AttachmentInstance`s with 5 distinct `content_hash` values (`h1..h5`), each backed by distinct real payload bytes.
2. Simulate a `BatchedRequestsResult<AttachmentRequest>` where all 5 `AttachmentRequest`s (for `h1..h5`) "succeed" against the single malicious peer, each returning an identical `GetAttachmentResponse { attachment: Attachment::new(fixed_bytes) }` (i.e., not matching 4 of the 5 real hashes).
3. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments.len() == 1`.
4. Drive `AttachmentsBatchStateMachine::Done` handling (mirroring `AttachmentsDownloader::run`'s `Done` branch) and assert that `find_all_attachment_instances` only returns/pairs the instance whose `content_hash == Hash160::from_data(fixed_bytes)`, while the other 4 `AttachmentInstance`s remain unresolved in `atlasdb`, and that `attachments_batch.has_fully_succeed()` is `false`, leading to re-enqueue/eventual drop after `max_attachment_retry_count` cycles.

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

**File:** stackslib/src/net/atlas/download.rs (L187-205)
```rust
                // Re-insert AttachmentsBatch back to the queue if not fully processed
                if !context.attachments_batch.has_fully_succeed() {
                    context.attachments_batch.bump_retry_count();
                    // If max_attachment_retry_count not reached, we'll re-enqueue the batch
                    if context.attachments_batch.retry_count
                        < context.connection_options.max_attachment_retry_count
                    {
                        info!(
                            "Atlas: re-enqueuing batch {:?} for retry",
                            context.attachments_batch
                        );
                        self.priority_queue.push(context.attachments_batch.clone());
                    } else {
                        info!(
                            "Atlas: dropping batch {:?} retries count exceeded",
                            context.attachments_batch
                        );
                    }
                }
```

**File:** stackslib/src/net/atlas/download.rs (L342-353)
```rust
pub struct AttachmentsBatchStateContext {
    pub attachments_batch: AttachmentsBatch,
    pub peers: HashMap<UrlString, ReliabilityReport>,
    pub connection_options: ConnectionOptions,
    pub dns_lookups: HashMap<UrlString, Option<Vec<SocketAddr>>>,
    pub inventories: HashMap<
        (QualifiedContractIdentifier, Vec<u32>, StacksBlockId),
        HashMap<UrlString, GetAttachmentsInvResponse>,
    >,
    pub attachments: HashSet<Attachment>,
    pub events_to_deregister: Vec<usize>,
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

**File:** stackslib/src/net/atlas/download.rs (L534-552)
```rust
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

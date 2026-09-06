### Title
Unverified attacment inventory bit inflates a lying peer's `ReliabilityReport` and stalls attachment/BNS resolution - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`get_prioritized_attachments_requests` derives `has_attachment` purely from the remote peer's self-reported `AttachmentPage.inventory` bit with no cross-check, and the surrounding reliability-scoring logic rewards a peer merely for returning a well-formed `GetAttachmentsInvResponse` (regardless of truthfulness), while a subsequent 404 on the actual `GET /v2/attachments/<hash>` never penalizes that peer's score. A remote peer can therefore always claim it has every attachment, get selected as the "most reliable source" every retry cycle, and stall resolution of the corresponding `AttachmentInstance` (e.g. a BNS name's zonefile) until `max_attachment_retry_count` is exhausted.

### Finding Description
In `AttachmentsBatchStateContext::get_prioritized_attachments_requests` (stackslib/src/net/atlas/download.rs:404-478), `has_attachment` is computed solely from the inventory bit supplied in the peer's `GetAttachmentsInvResponse`: [1](#0-0) 
No comparison is made against any locally-verifiable fact (e.g., a prior successful fetch); any peer that sets the bit to 1 is inserted into `sources` for the `AttachmentRequest`.

Compounding this, `extend_with_inventories` (download.rs:490-528) bumps the lying peer's `ReliabilityReport` upward on every 200 OK / well-formed inventory response, independent of the inventory's truthfulness: [2](#0-1) 

When the actual content request `GET /v2/attachments/<hash>` is later dispatched, a 404 response is routed to `faulty_peers` and never reaches `succeeded`, so the peer's `ReliabilityReport` is left untouched by `extend_with_attachments` (download.rs:530-558) for that failure: [3](#0-2) [4](#0-3) 

Since `AttachmentRequest::get_url()` and its `Ord` impl pick the source with the highest `ReliabilityReport::score()` (download.rs:1073-1096), a peer that always answers inventory queries truthfully-formatted-but-falsely-populated inflates its score every cycle while never being decremented for the ensuing 404, making it the preferred/only source repeatedly. The batch is not resolved, `AttachmentsBatch::bump_retry_count()` re-queues it with exponential backoff up to `connection_options.max_attachment_retry_count` (download.rs:183-205), after which it is dropped: [5](#0-4) 

### Impact Explanation
The victim node's attachment resolution (used for BNS zonefile fetch/name resolution) is stalled for the bounded retry window and ultimately fails for that name, while compute/network cycles are wasted requesting the same lying peer each round because its score is never penalized relative to honest peers. This matches the allowed "bounded compute DoS on a read endpoint" / attachment-BNS-mismatch category: the requesting client is misled into repeatedly trusting a peer with no real data, degrading BNS resolution availability without requiring volumetric traffic — a single dishonest response per retry cycle suffices.

### Likelihood Explanation
The attacker only needs to be an outbound sync peer of the victim (achievable simply by running a normal node and being gossiped/connected to, per the unprivileged-attacker model) and to answer `/v2/attachments/inv` requests with a fabricated bit set to 1 for the targeted `attachment_index`. No secret, signature, or privileged role is required; the attack is fully repeatable each retry cycle at negligible attacker cost.

### Recommendation
- Penalize `ReliabilityReport` for peers whose claimed inventory bit does not correspond to an actually deliverable attachment (e.g., bump failed requests on the peer's report when the subsequent content fetch 404s/faults, not just when the inv request itself fails to decode).
- Consider excluding a peer from `sources` for a given content hash after repeated failed content fetches despite claimed possession, independent of inventory-request success.

### Proof of Concept
Net test plan:
1. Set up a mocked peer with a data URL that responds 200 to `GET /v2/attachments/inv?...` with a `GetAttachmentsInvResponse` whose `AttachmentPage.inventory[position_in_page] = 1` for a target `content_hash` X, and responds 404 to `GET /v2/attachments/X`.
2. Build an `AttachmentsBatch` containing an `AttachmentInstance` for X, run `AttachmentsDownloader::run` across multiple cycles.
3. Assert that `get_prioritized_attachments_requests` includes the lying peer in `sources` for X each cycle (download.rs:434-459), that its `ReliabilityReport.total_requests_success` climbs from `extend_with_inventories` while `extend_with_attachments`/`faulty_peers` never decrements it for the 404 (download.rs:507-522, 899-904).
4. Assert `AttachmentsBatch::bump_retry_count()` is invoked and the batch is re-queued up to `connection_options.max_attachment_retry_count` before being dropped (download.rs:188-205), confirming the bounded resolution stall.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L437-452)
```rust
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
```

**File:** stackslib/src/net/atlas/download.rs (L507-522)
```rust
            if let Ok(response) = response.decode_atlas_attachments_inv_response() {
                let peer_url = request.get_url().clone();
                match self.inventories.entry(request.key()) {
                    Entry::Occupied(responses) => {
                        responses.into_mut().insert(peer_url, response);
                    }
                    Entry::Vacant(v) => {
                        let mut responses = HashMap::new();
                        responses.insert(peer_url, response);
                        v.insert(responses);
                    }
                };
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
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

**File:** stackslib/src/net/atlas/download.rs (L899-904)
```rust
                                    Some(response) => {
                                        let peer_url = request.get_url().clone();
                                        if response.preamble().status_code == 404 {
                                            state.faulty_peers.insert(event_id, peer_url);
                                            continue;
                                        }
```

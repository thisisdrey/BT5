### Title
Malicious Atlas data-server peer can return an unbounded number of `AttachmentPage` entries in `GetAttachmentsInvResponse`, causing unbounded client-side compute in `get_prioritized_attachments_requests` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
The server-side handler `RPCGetAttachmentsInvRequestHandler::try_handle_request` enforces `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) on the *request*, but the client-side response decoder `StacksHttpResponse::decode_atlas_attachments_inv_response` and the downstream consumers `AttachmentsBatchStateContext::extend_with_inventories` / `get_prioritized_attachments_requests` never verify that `response.pages.len()` is bounded. A malicious peer that a node is honestly syncing Atlas attachments with can return a `GetAttachmentsInvResponse` with an arbitrarily large `pages` vector, and the client will store and iterate over it in full.

### Finding Description
`get_paginated_missing_pages_for_contract_id` builds outbound `AttachmentsInventoryRequest.pages` batches of at most `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` entries, and the server enforces this cap on the *request* side in `stackslib/src/net/api/getattachmentsinv.rs:159-168`. However, the enforcement is entirely one-directional: it limits how many pages a well-behaved server will compute and return for a *conforming* request, but nothing stops a malicious peer's HTTP server implementation from returning an arbitrarily large JSON body for the `/v2/attachments/inv` GET request/response cycle that the requesting node parses.

The client-side decode path is: [1](#0-0) 
which deserializes the response body directly into `GetAttachmentsInvResponse` via `serde_json::from_value` with no post-deserialization length check on `pages`.

This response is then stored unconditionally per-peer in `extend_with_inventories`: [2](#0-1) 

And later, `get_prioritized_attachments_requests` iterates every `(contract_id, pages, _)` inventory entry, and for every missing attachment, over every `peers_responses`, doing a linear `response.pages.iter().find(...)` scan: [3](#0-2) 

Because `response.pages` is attacker-controlled and unbounded, this `find` call becomes O(pages) per (attachment, peer) pair, giving overall complexity of O(missing_attachments × num_peers × attacker_page_count) instead of the intended O(missing_attachments × num_peers × 8).

This whole pipeline (`AttachmentsDownloader::run` → `AttachmentsBatchStateMachine::try_proceed` → `extend_with_inventories`/`get_prioritized_attachments_requests`) executes inside the P2P event loop: [4](#0-3) 

### Impact Explanation
A malicious data-server peer that the victim node is syncing attachments from (a normal outbound sync peer providing a `data_url`, requiring no privileged role — this is standard Atlas peer behavior) can inflate `AttachmentsInvResponse.pages` far beyond the intended cap of 8. Each such response causes extra linear-scan work in the P2P thread's `get_prioritized_attachments_requests`, scaling with attacker-supplied page count. This is a bounded compute-cost amplification on the P2P event loop, consistent with a "bounded compute DoS" category, though the amplification factor is modest per single HTTP response (bounded by response body size limits inherited from the general HTTP/JSON parsing stack, which were not fully verified within available context) rather than unbounded/critical.

### Likelihood Explanation
Precondition: the attacker must be a peer that the victim has selected as an outbound Atlas sync peer with an advertised `data_url` — this only requires being a normal, reachable P2P participant, no special privilege or secret. The attacker can repeatedly trigger this on every attachment-inventory sync round the victim performs against them, at the cost of running their own HTTP responder. Reachability is confirmed since this is the standard `/v2/attachments/inv` RPC response path used during ordinary Atlas synchronization.

### Recommendation
Enforce an explicit cap on `response.pages.len()` (e.g., equal to `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`, or bound to the number of unique page indices requested) immediately after decoding in `decode_atlas_attachments_inv_response`, or in `extend_with_inventories` before inserting the response into `self.inventories`, rejecting/truncating and treating the peer as faulty (bumping `report.bump_failed_requests()`) on excess.

### Proof of Concept
Rust test in `stackslib::net::atlas::tests` mirroring `test_downloader_context_attachment_requests`:
1. Build an `AttachmentsBatchStateContext` with one contract and a handful of missing attachments, as in `stackslib/src/net/atlas/tests.rs:669-728`.
2. Construct a crafted `GetAttachmentsInvResponse` (bypassing the HTTP layer, directly as done via `new_attachments_inventory_response`) containing 10,000 `AttachmentPage` entries with arbitrary indices instead of ≤8.
3. Insert it into `BatchedRequestsResult.succeeded` for one peer, call `context.extend_with_inventories(&mut inventories_results)`, and confirm no error/rejection occurs (i.e., `self.inventories` accepts the oversized response unchanged, `pages.len() == 10000`).
4. Call `context.get_prioritized_attachments_requests()` and measure wall-clock versus an equivalent response with 8 pages, asserting no equality/cap check (`response.pages.len() <= MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`) is applied anywhere in the call path, and that runtime scales roughly linearly with the injected page count.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L253-262)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_attachments_inv_response(
        self,
    ) -> Result<GetAttachmentsInvResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentsInvResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

**File:** stackslib/src/net/atlas/download.rs (L404-459)
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
```

**File:** stackslib/src/net/atlas/download.rs (L490-523)
```rust
    pub fn extend_with_inventories(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentsInventoryRequest>,
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
        }
```

**File:** stackslib/src/net/atlas/download.rs (L618-640)
```rust
            AttachmentsBatchStateMachine::DownloadingAttachmentsInv((
                attachments_invs_requests,
                context,
            )) => {
                match BatchedRequestsState::try_proceed(
                    attachments_invs_requests,
                    &context.dns_lookups,
                    network,
                    &context.connection_options,
                ) {
                    BatchedRequestsState::Done(ref mut results) => {
                        let context = context.extend_with_inventories(results);
                        let sub_state = {
                            let requests_queue = context.get_prioritized_attachments_requests();
                            BatchedRequestsState::BeginRequests(Some(requests_queue), None)
                        };
                        AttachmentsBatchStateMachine::DownloadingAttachment((sub_state, context))
                    }
                    state => {
                        AttachmentsBatchStateMachine::DownloadingAttachmentsInv((state, context))
                    }
                }
            }
```

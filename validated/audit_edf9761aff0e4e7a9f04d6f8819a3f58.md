Confirmed: `extend_with_inventories` at `stackslib/src/net/atlas/download.rs:490-528` decodes the response via `decode_atlas_attachments_inv_response()` and stores it keyed by `request.key()` — which is derived from the *request's* `(contract_id, pages, index_block_hash)` — without ever comparing `response.block_id` to `request.index_block_hash`.

### Title
Missing verification that `GetAttachmentsInvResponse.block_id` matches the requested `index_block_hash`, allowing inventory poisoning - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_inventories` accepts any JSON-decodable `GetAttachmentsInvResponse` from a peer and stores it keyed by the *request's* `(contract_id, pages, index_block_hash)` tuple, never checking that the peer's `response.block_id` equals the `index_block_hash` that was actually requested. A malicious peer can therefore answer a query for block A's attachment inventory with fabricated page data associated (in its own response) with a different block, and that data will be indexed and trusted as if it were block A's inventory.

### Finding Description
The request is built in `get_prioritized_attachments_inventory_requests` (`download.rs:376-402`) with a fixed `index_block_hash: self.attachments_batch.index_block_hash`. The peer's HTTP handler `RPCGetAttachmentsInvRequestHandler::try_handle_request` (`getattachmentsinv.rs:135-218`) is supposed to echo back `block_id: index_block_hash.clone()` (line 210-213) taken from the *server's* own atlas DB lookup for the requested `index_block_hash`. However, this is only the honest-server behavior; a malicious peer controls its own server code/response bytes entirely (attacker "run their own peer") and can send an arbitrary JSON body for `GET /v2/attachments/inv` with any `block_id` and fabricated `pages`/`inventory` bit-vectors.

On the requester side, `decode_atlas_attachments_inv_response()` (`getattachmentsinv.rs:253-262`) only performs `parse_json` + `serde_json::from_value` — pure structural JSON decoding with no cross-check against the outbound request. Then in `extend_with_inventories` (`download.rs:507-518`):
```
if let Ok(response) = response.decode_atlas_attachments_inv_response() {
    let peer_url = request.get_url().clone();
    match self.inventories.entry(request.key()) { ... insert response ... }
```
The map is keyed by `request.key()` (derived from `AttachmentsInventoryRequest`'s `contract_id, pages, index_block_hash`), and the value stored is the *attacker-controlled* `response`, whose own `block_id` field is never compared to `request.index_block_hash`. This breaks the invariant that a stored inventory response is guaranteed to pertain to the block context it is keyed under.

### Impact Explanation
Downstream, `get_prioritized_attachments_requests` (`download.rs:404-478`) consumes `self.inventories` entries and, for each peer's response, inspects `response.pages[...].inventory` bitmaps to decide whether that peer has a given attachment (matched by content hash from the node's own `attachments_batch`, not from the response). The `block_id` field itself is not used for attachment-fetch decisioning in this code path — the poisoning practically manifests as the peer being able to supply arbitrary inventory bitmaps (claiming presence/absence of pages) regardless of what block context it was actually asked about, since nothing forces the returned bitmap data to correspond to the requested block. This lets a malicious peer influence which peers get contacted for attachment content (`AttachmentRequest.sources`) by falsely claiming to have (or not have) an attachment, potentially steering fetch attempts toward itself or away from honest peers. This is a data-integrity/inventory-poisoning issue in the Atlas attachment-sync subsystem, but it does not cause consensus-state corruption, remote crash, or unauthenticated state writes — the actual attachment content is still separately validated against its content hash via `find_attachment`/`insert_instantiated_attachment` flows, so no invalid attachment blob is accepted as canonical merely from this response.

### Likelihood Explanation
Any remote peer already listed among `network.get_outbound_sync_peers()` with a reachable data URL can trigger this by simply answering `/v2/attachments/inv` GET requests with a crafted JSON body; no secret, signature, or privileged role is needed, and this is a normal RPC endpoint. However, the actual impact is limited: it can only influence which peer is asked for attachment bytes (an efficiency/reliability degradation), not force invalid data acceptance, since attachment content is still validated by hash before being trusted (`check_attachment_instances`, `download.rs:227-286`).

### Recommendation
In `extend_with_inventories`, after decoding the response, assert `response.block_id == request.index_block_hash` before inserting into `self.inventories`; on mismatch, treat it as a failed/faulty response (`report.bump_failed_requests()` and optionally mark faulty peer) rather than silently storing it.

### Proof of Concept
```rust
// stackslib/src/net/atlas/tests.rs (net test sketch)
let request = AttachmentsInventoryRequest {
    url: peer_url.clone(),
    contract_id: contract_id.clone(),
    pages: vec![0],
    stacks_block_height: 1,
    index_block_hash: block_a.clone(), // requested block A
    reliability_report: ReliabilityReport::empty(),
    canonical_stacks_tip_height: None,
};
let fabricated_response = GetAttachmentsInvResponse {
    block_id: block_b.clone(), // different, non-committed block
    pages: vec![AttachmentPage { index: 0, inventory: vec![1] }],
};
// simulate BatchedRequestsResult::succeeded containing (request, Some(http_response_with(fabricated_response)))
let ctx = ctx.extend_with_inventories(&mut results);
let stored = ctx.inventories.get(&(contract_id, vec![0], block_a)).unwrap();
// assert stored response's inner block_id is block_b, proving no cross-check occurred
assert_eq!(stored.get(&peer_url).unwrap().block_id, block_b);
```
This demonstrates that `extend_with_inventories` (`download.rs:490-528`) stores a response whose `block_id` diverges from the request's `index_block_hash` used as the map key, confirming the missing equality check. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** stackslib/src/net/atlas/download.rs (L376-402)
```rust
    pub fn get_prioritized_attachments_inventory_requests(
        &self,
    ) -> BinaryHeap<AttachmentsInventoryRequest> {
        let mut queue = BinaryHeap::new();
        for (contract_id, _) in self.attachments_batch.attachments_instances.iter() {
            let pages_batches = self
                .attachments_batch
                .get_paginated_missing_pages_for_contract_id(contract_id);
            for (peer_url, reliability_report) in self.peers.iter() {
                for pages in pages_batches.iter() {
                    let request = AttachmentsInventoryRequest {
                        url: peer_url.clone(),
                        reliability_report: reliability_report.clone(),
                        contract_id: contract_id.clone(),
                        pages: pages.clone(),
                        stacks_block_height: self.attachments_batch.stacks_block_height,
                        index_block_hash: self.attachments_batch.index_block_hash.clone(),
                        canonical_stacks_tip_height: self
                            .attachments_batch
                            .canonical_stacks_tip_height,
                    };
                    queue.push(request);
                }
            }
        }
        queue
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

**File:** stackslib/src/net/atlas/download.rs (L490-528)
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
        let mut events_ids = results.faulty_peers.keys().copied().collect::<Vec<usize>>();
        self.events_to_deregister.append(&mut events_ids);

        self
    }
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L141-213)
```rust
        let index_block_hash = self
            .index_block_hash
            .take()
            .ok_or(NetError::SendError("Missing `index_block_hash`".into()))?;
        let page_indexes = self
            .page_indexes
            .take()
            .ok_or(NetError::SendError("Missing `page_indexes`".into()))?;

        // We are receiving a list of page indexes with a chain tip hash.
        // The amount of pages_indexes is capped by MAX_ATTACHMENT_INV_PAGES_PER_REQUEST (8)
        // Pages sizes are controlled by the constant ATTACHMENTS_INV_PAGE_SIZE (8), which
        // means that a `GET v2/attachments/inv` request can be requesting for a 64 bit vector
        // at once.
        // Since clients can be asking for non-consecutive pages indexes (1, 5_000, 10_000, ...),
        // we will be handling each page index separately.
        // We could also add the notion of "budget" so that a client could only get a limited number
        // of pages when they are spanning over many blocks.
        if page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST {
            let msg = format!(
                "Number of attachment inv pages is limited by {} per request",
                MAX_ATTACHMENT_INV_PAGES_PER_REQUEST
            );
            warn!("{msg}");
            return StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new(msg))
                .try_into_contents()
                .map_err(NetError::from);
        }
        if page_indexes.is_empty() {
            let msg = "Page indexes missing".to_string();
            warn!("{msg}");
            return StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new(msg))
                .try_into_contents()
                .map_err(NetError::from);
        }

        let mut pages = vec![];

        for page_index in page_indexes.iter() {
            let page_res =
                node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                    match network
                        .get_atlasdb()
                        .get_attachments_available_at_page_index(*page_index, &index_block_hash)
                    {
                        Ok(inventory) => Ok(AttachmentPage {
                            inventory,
                            index: *page_index,
                        }),
                        Err(e) => {
                            let msg = format!("Unable to read Atlas DB - {}", e);
                            warn!("{}", msg);
                            Err(msg)
                        }
                    }
                });

            match page_res {
                Ok(page) => {
                    pages.push(page);
                }
                Err(msg) => {
                    return StacksHttpResponse::new_error(&preamble, &HttpNotFound::new(msg))
                        .try_into_contents()
                        .map_err(NetError::from);
                }
            }
        }

        let content = GetAttachmentsInvResponse {
            block_id: index_block_hash.clone(),
            pages,
        };
```

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

### Title
Missing content-hash verification on `GetAttachmentResponse` allows attacker-controlled `Attachment` to be stored under wrong hash and leaves requested attachment permanently unresolved - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `GetAttachmentResponse` returned for an `AttachmentRequest` without checking that `Attachment.content` actually hashes to the requested `content_hash`. The `Done` branch in `AttachmentsDownloader::run` then indexes/resolves purely by `attachment.hash()` (the hash recomputed from the served bytes), so a malicious peer can return arbitrary bytes for a legitimate content_hash request and the requested `AttachmentInstance` will never resolve.

### Finding Description
The claimed equality — that the state machine relies on `served_attachment.hash() == AttachmentRequest.content_hash` — does not hold anywhere in the code, and nothing enforces it either:

- `AttachmentsBatchStateContext::get_prioritized_attachments_requests` builds an `AttachmentRequest { content_hash, .. }` from the on-chain-verified `AttachmentInstance.content_hash` and issues `GET /v2/attachments/{content_hash}` [1](#0-0) .
- The response is decoded in `extend_with_attachments`, which calls `response.decode_atlas_get_attachment()` and unconditionally inserts `response.attachment` into `self.attachments` (a `HashSet<Attachment>`) — there is no comparison against `request.content_hash` at all: [2](#0-1) .
- `RPCGetAttachmentRequestHandler::decode_atlas_get_attachment` simply JSON-parses the body into `GetAttachmentResponse` with zero hash validation: [3](#0-2) .
- In the `Done` branch of `AttachmentsDownloader::run`, attachments are drained and both `find_all_attachment_instances` and `resolve_attachment` are keyed by `attachment.hash()` (i.e., recomputed from whatever bytes the peer sent), not by the originally requested `content_hash`: [4](#0-3) .

Because of this, a peer that receives `GET /v2/attachments/H` can respond `200 OK` with `{"content": <bytes with Hash160 = H'>}` where `H' != H`. The code will call `network.atlasdb.insert_instantiated_attachment(&attachment)` for `H'`, and `context.attachments_batch.resolve_attachment(&H')` — which does nothing useful for the batch's outstanding entry for `H`, since `resolve_attachment` marks off the `content_hash` it's given, not the one actually requested. The `AttachmentInstance` originally tracked under `H` remains unresolved in `attachments_batch`, so `has_fully_succeed()` stays false and the batch is retried until `max_attachment_retry_count`, after which it is dropped permanently (`Atlas: dropping batch ... retries count exceeded`) [5](#0-4) .

No guard exists at any layer (HTTP handler, response decoder, or state-machine merge step) that checks `Hash160::from_data(&attachment.content) == request.content_hash` before acceptance.

### Impact Explanation
An unprivileged peer that a victim node treats as an outbound sync peer (any node the victim happens to select for Atlas sync — no special privilege or secret required) can serve wrong bytes for a legitimately on-chain-committed attachment hash. This causes:
- The victim's AtlasDB to store an unrelated attachment blob under `H'` (attacker-chosen "garbage" indexed data, though bounded to the interface `insert_instantiated_attachment` expects).
- The correctly-tracked `AttachmentInstance` for hash `H`, which corresponds to an on-chain-committed BNS/attachment record, is retried and eventually dropped as "unresolved," causing the node to treat a canonically-committed attachment as permanently absent for BNS resolution purposes.

This matches "High - serving non-canonical state as canonical, attachment/BNS mismatch" since the victim node ends up believing a valid, chain-committed attachment does not exist, purely because one queried peer returned mismatched content.

### Likelihood Explanation
- Attacker only needs to run an ordinary Atlas peer that the victim selects as an outbound sync peer for that data URL — this is a normal, unprivileged network role reachable via P2P/RPC without any secret, admin role, or local access.
- The attacker needs to be selected among `network.get_outbound_sync_peers()` and have advertised (via `GetAttachmentsInvResponse`) that it holds attachment `H`, which is easy for a self-run peer to fake since the inventory response is similarly not hash-verified beyond bitmap semantics.
- Attack is cheap and repeatable: each targeted attachment request just needs one crafted 200 response.
- If other correctly-behaving peers are also queried for `H` in the same batch (multiple sources are tracked via `sources: HashMap<UrlString, ReliabilityReport>`), a legitimate response might overwrite the bad entry in the `HashSet` in a later poll cycle, but the vulnerability is deterministic in the sole-source or first-server-wins scenario per the code as written, since `extend_with_attachments` performs no hash check regardless of source count.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs, around line 547), before inserting `response.attachment`, compute `Hash160::from_data(&response.attachment.content)` (or call `attachment.hash()`) and compare it to `request.content_hash`. Discard/treat as faulty (`report.bump_failed_requests()`, and optionally mark peer as `faulty_peers`) any response whose computed hash does not match the requested hash, rather than blindly trusting served bytes.

### Proof of Concept
Rust test in `stackslib::net::atlas` (e.g. add to `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AttachmentRequest` with a known `content_hash = H` (e.g., hash of `b"expected"`).
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains `(request, Some(mocked_response))`, where `mocked_response` is a `StacksHttpResponse` wrapping a `GetAttachmentResponse { attachment: Attachment { content: b"malicious".to_vec() } }` (so `attachment.hash() = H' != H`).
3. Call `AttachmentsBatchStateContext::extend_with_attachments(context, &mut results)`.
4. Assert `context.attachments` contains the attachment keyed at `H'` (via `attachment.hash()`), and that no logic anywhere compared it to `H`.
5. Simulate the `Done` branch logic (as in `AttachmentsDownloader::run`) and assert that after processing, `atlasdb.find_attachment(&H)` is still `None`/`Ok(None)` (i.e., the originally requested attachment is unresolved), while `atlasdb.find_attachment(&H')` returns `Some(..)` with attacker content — proving the mismatch is stored and the real attachment stays unresolved indefinitely until batch retries exhaust and the batch is dropped.

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

**File:** stackslib/src/net/atlas/download.rs (L404-475)
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

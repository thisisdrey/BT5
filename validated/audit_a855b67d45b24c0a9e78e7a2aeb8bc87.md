### Title
Malicious peer can serve mismatched-hash `Attachment` content and permanently prevent resolution of the real on-chain attachment commitment - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any HTTP 200 `GetAttachmentResponse` from a peer and inserts `response.attachment` into `self.attachments` without ever checking that `response.attachment.hash() == request.content_hash`. In the `Done` branch of `AttachmentsDownloader::run`, resolution is then keyed off the served attachment's own hash (`attachment.hash()`), not the originally requested `content_hash`, so a malicious outbound-sync peer can serve unrelated content and cause the real attachment commitment to never resolve.

### Finding Description
The broken equality: for a served `Attachment`, the code never asserts `served_attachment.hash() == request.content_hash`.

- `AttachmentRequest::make_request_type` builds a GET to `/v2/attachments/{content_hash}` [1](#0-0) .
- The remote peer's response is decoded purely via JSON deserialization with no hash check: `StacksHttpResponse::decode_atlas_get_attachment` just parses `GetAttachmentResponse` from the body [2](#0-1) .
- `AttachmentsBatchStateContext::extend_with_attachments` drains the `succeeded` results and, for any response that decodes, does `self.attachments.insert(response.attachment)` — again with no comparison to `request.content_hash` (the `request` variable is discarded after fetching the peer's `ReliabilityReport`) [3](#0-2) .
- In the `Done` state of `AttachmentsDownloader::run`, resolution is performed per served attachment using `attachment.hash()` (the hash of the possibly-forged content), not the original `content_hash` that was requested: `network.atlasdb.find_all_attachment_instances(&attachment.hash())` and `context.attachments_batch.resolve_attachment(&attachment.hash())` [4](#0-3) .
- `AttachmentsBatch.attachments_instances` is a map keyed by `(contract_id, attachment_index) -> content_hash`; entries are only removed when `resolve_attachment` is called with the matching real `content_hash` value [5](#0-4) .

Exploit flow: the attacker (an outbound-sync peer the node is talking to, or the only peer claiming to have the attachment in its inventory) responds to the `GetAttachmentsInv` request truthfully claiming to have bit=1 for the target page/index so it becomes a `source` in the resulting `AttachmentRequest` [6](#0-5) . When the node later issues `GET /v2/attachments/{real_content_hash}` to this peer, the attacker returns HTTP 200 with an arbitrary `Attachment{content}` whose `hash()` differs from `real_content_hash`. Because no check compares `attachment.hash()` to the request's `content_hash`, the forged attachment is stored via `insert_instantiated_attachment` under its own (wrong) hash, `find_all_attachment_instances(&wrong_hash)` returns zero matches, and `resolve_attachment(&wrong_hash)` removes nothing from `attachments_instances`, leaving the real `content_hash` entry present. `has_fully_succeed()` remains false, so the batch is bumped and re-queued for retry; if the attacker (or the only peer serving this data) repeats the mismatch on every retry, the batch is eventually dropped after `max_attachment_retry_count` retries is exceeded (`"Atlas: dropping batch ... retries count exceeded"`) [7](#0-6) . Since `AttachmentsDownloader` is created once and its `initial_batch` (from `find_unresolved_attachment_instances`) is only consumed once at construction [8](#0-7) , once the batch is dropped from the retry queue there is no other periodic mechanism shown in this code that re-enqueues the same already-tracked, unresolved attachment instance — leaving the BNS name's attachment perpetually unresolved on this node until a restart or a new triggering event.

### Impact Explanation
The victim node never resolves a legitimately on-chain-committed BNS/Atlas attachment even though its content_hash was genuinely announced and is being served in good faith by other honest peers (if the attacker is a chosen source, or the sole source, for that content_hash). This matches the "attachment/BNS mismatch" High-severity category: it causes the node to persistently miss/fail-to-serve state that was genuinely committed on-chain, without any state corruption of consensus data, but a durable availability/correctness gap for that specific attachment on the targeted node. It is repeatable per attachment/content_hash and per victim node that selects the attacker as an outbound-sync peer/source.

### Likelihood Explanation
- Attacker needs only to be an outbound-sync peer of the victim (no special key, no StackerDB slot, no RPC secret) — reachable via the standard P2P/RPC attachment-sync flow.
- Attacker must be selected as (one of) the `sources` for the target `content_hash`, achieved simply by returning `bit=1` for that page in its (honest) `GetAttachmentsInvResponse`, then it becomes eligible as `get_most_reliable_source()` for the follow-up `GetAttachment` request; being the sole/most-reliable source maximizes reliability but is not strictly required — any single accepted 200 response with a mismatched attachment is enough to make that download attempt fail to resolve the true hash for that retry cycle.
- Attacker cost is a single crafted HTTP 200 response per retry cycle (repeatable, cheap).
- Preconditions: the specific attachment instance must be currently unresolved on the victim and the attacker peer must be reachable/connected as an outbound-sync peer providing inventory data — reasonably attainable for any node the attacker can peer with.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; if mismatched, treat it as a failed request (`report.bump_failed_requests()`), optionally penalize/deregister the peer, and keep the request eligible for retry against another source. Do not use `attachment.hash()` for resolution in the `Done` branch of `AttachmentsDownloader::run` — key `find_all_attachment_instances` and `AttachmentsBatch::resolve_attachment` off the originating request's `content_hash` (kept alongside the response) rather than the served content's self-computed hash.

### Proof of Concept
Rust unit test (add to `stackslib/src/net/atlas/tests.rs`), modeled on `test_downloader_context_attachment_requests`:
```rust
#[test]
fn test_extend_with_attachments_rejects_hash_mismatch() {
    let real_attachment = new_attachment_from("facade01");
    let real_hash = real_attachment.hash();

    let forged_attachment = Attachment::new(b"evil-content".to_vec());
    assert_ne!(forged_attachment.hash(), real_hash);

    let attachments_batch = new_attachments_batch_from(
        vec![new_attachment_instance_from(&real_attachment, attachment_index(0, 0), 1)],
        0,
    );
    let peers = new_peers(vec![("http://localhost:20443", 1, 1)]);
    let context = AttachmentsBatchStateContext::new(
        attachments_batch, peers, &ConnectionOptions::default(),
    );

    let request = new_attachment_request(
        vec![("http://localhost:20443", 1, 1)],
        &real_hash,
        1,
    );

    // Simulate a malicious peer's HTTP 200 response carrying mismatched content.
    let forged_response = new_getattachment_response(forged_attachment.clone());
    let mut results = BatchedRequestsResult::empty();
    results.succeeded.insert(request, Some(forged_response));

    let context = context.extend_with_attachments(&mut results);

    // BUG: forged attachment gets stored regardless of hash mismatch.
    assert!(context.attachments.contains(&forged_attachment));

    // Expected/desired behavior (currently FAILS): the real content_hash should
    // still be tracked as unresolved, since the served content did not match.
    assert!(context
        .attachments_batch
        .attachments_instances
        .values()
        .any(|m| m.values().any(|h| *h == real_hash)));
}
```
This asserts that `extend_with_attachments` stores the forged attachment, and that `AttachmentsBatch::resolve_attachment` is never invoked for the real `content_hash` (it stays present in `attachments_instances`), demonstrating the permanent-unresolved condition described.

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

**File:** stackslib/src/net/atlas/download.rs (L430-472)
```rust
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
```

**File:** stackslib/src/net/atlas/download.rs (L534-553)
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
        }
```

**File:** stackslib/src/net/atlas/download.rs (L1110-1118)
```rust
    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".to_string(),
            format!("/v2/attachments/{}", &self.content_hash),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1128-1136)
```rust
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct AttachmentsBatch {
    pub stacks_block_height: u64,
    pub canonical_stacks_tip_height: Option<u64>,
    pub index_block_hash: StacksBlockId,
    pub attachments_instances: HashMap<QualifiedContractIdentifier, HashMap<u32, Hash160>>,
    pub retry_count: u64,
    pub retry_deadline: u64,
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

**File:** stackslib/src/net/p2p.rs (L4127-4140)
```rust
        if self.attachments_downloader.is_none() {
            self.atlasdb
                .evict_expired_uninstantiated_attachments()
                .expect("FATAL: atlasdb error: evict_expired_uninstantiated_attachments");
            self.atlasdb
                .evict_expired_unresolved_attachment_instances()
                .expect("FATAL: atlasdb error: evict_expired_unresolved_attachment_instances");
            let initial_batch = self
                .atlasdb
                .find_unresolved_attachment_instances()
                .expect("FATAL: atlasdb error: find_unresolved_attachment_instances");

            self.init_attachments_downloader(initial_batch);
        }
```

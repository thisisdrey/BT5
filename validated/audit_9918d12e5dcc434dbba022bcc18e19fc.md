### Title
Attacker-controlled peer inventory bit is trusted with zero content-hash verification, letting a single malicious peer permanently starve `AttachmentsBatch` resolution - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::get_prioritized_attachments_requests` populates an `AttachmentRequest`'s `sources` map purely from the advertised inventory bit in `GetAttachmentsInvResponse.pages[..].inventory`, with no cross-check against actual servable bytes. [1](#0-0)  Worse than the question assumes: `extend_with_attachments` never compares the returned `Attachment`'s hash to the requested `content_hash` at all, so a peer serving forged/mismatched bytes is recorded as a *successful* request rather than a failed one. [2](#0-1) 

### Finding Description
The claimed equality "advertised availability == actual servable byte-for-hash match" is indeed broken: in `get_prioritized_attachments_requests`, any peer whose `AttachmentPage.inventory[position_in_page] == 1` is inserted into `sources` with no verification step, and if that peer is the only one advertising the bit, it becomes the sole entry in `sources`, and thus the value returned by `get_most_reliable_source`/`get_url` for the `Requestable` implementation of `AttachmentRequest`. [3](#0-2) 

However, the question's specific downstream claim — that a hash mismatch causes `report.bump_failed_requests()` to fire — does not match the code. `extend_with_attachments` only calls `decode_atlas_get_attachment()`, which merely parses the JSON body into a `GetAttachmentResponse` with no comparison to the requested `content_hash`. [4](#0-3)  As long as the JSON decodes, `report.bump_successful_requests()` is called and the (possibly bogus) `Attachment` is inserted into `context.attachments`, regardless of whether its content matches what was requested. [5](#0-4) 

The actual failure surfaces later in `AttachmentsDownloader::run`: for each entry in `context.attachments`, `attachment.hash()` (the *actual* hash of the bytes returned) is used to look up matching instances and to call `attachments_batch.resolve_attachment(&attachment.hash())`. [6](#0-5)  If the malicious peer served garbage whose hash differs from the requested `content_hash`, `resolve_attachment` silently finds no matching entry and the original missing attachment is never removed from `attachments_instances`, so the batch is not fully resolved and gets re-queued via `bump_retry_count()` until `max_attachment_retry_count` is exhausted, at which point it is dropped entirely. [7](#0-6) 

This is actually a more severe variant than described: because `bump_successful_requests()` is (incorrectly) invoked for the malicious peer, its `ReliabilityReport.score()` improves, making it *more* likely to be re-selected as the "most reliable source" in subsequent retries via `AttachmentRequest::get_most_reliable_source` and to be prioritized in `AttachmentsInventoryRequest`'s `Ord` (based on `reliability_report`). [8](#0-7) [9](#0-8)  There is no blacklisting mechanism anywhere in this file that excludes a peer whose delivered content hash mismatches what it advertised/what was requested.

### Impact Explanation
A single unprivileged remote peer that is (or becomes) the sole outbound peer advertising a given attachment's inventory bit can indefinitely prevent that attachment from being resolved: it is chosen as the only `sources` entry, its bogus response is treated as a reliability success, and the batch is retried until `max_attachment_retry_count` is exhausted and then dropped, without the real content ever being fetched even if a legitimate holder later becomes reachable in later retries (since the malicious peer's boosted score keeps it preferred). This matches the "High: attachment/BNS mismatch" category — BNS name/attachment resolution can be denied by a single malicious peer's false inventory advertisement.

### Likelihood Explanation
The attacker only needs one outbound-reachable connection slot to the victim (an ordinary unprivileged peer relationship) and control over its own `/v2/attachments/inv` and `/v2/attachments/:hash` RPC responses — both trivially forgeable since neither endpoint's response is authenticated or hash-checked against the requested content. No secret, admin role, or additional privilege is needed, and the attack is repeatable per attachment/batch.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs), after `decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before accepting it as success; on mismatch, call `report.bump_failed_requests()` and additionally record/quarantine the offending peer URL (e.g., a blacklist keyed by `UrlString`) so it is excluded from `sources` selection in future `get_prioritized_attachments_requests` calls, rather than only relying on `ReliabilityReport.score()`, which can be inflated by exactly this false-success bug.

### Proof of Concept
1. Set up a `PeerNetwork` test harness with `network.get_outbound_sync_peers()` returning a single malicious peer with a controlled HTTP RPC responder.
2. Have the malicious peer's `/v2/attachments/inv` handler return an `AttachmentsInvResponse` with `pages[i].inventory[j] = 1` for a `content_hash` `H` it does not actually possess.
3. Have the malicious peer's `/v2/attachments/{H}` handler return a `GetAttachmentResponse` whose `Attachment` content hashes to `H' != H`.
4. Drive `AttachmentsDownloader::run` through `Initialized -> DNSLookup -> DownloadingAttachmentsInv -> DownloadingAttachment -> Done` and assert:
   - `sources` for the `AttachmentRequest` for `H` contains only the malicious peer's URL (confirms `get_prioritized_attachments_requests` at stackslib/src/net/atlas/download.rs:430-464 trusts the bit unconditionally).
   - After `extend_with_attachments`, the malicious peer's `ReliabilityReport.total_requests_success` incremented (confirms no hash check at stackslib/src/net/atlas/download.rs:547-552).
   - `attachments_batch.attachments_instances` for `H` is still non-empty after `Done` (confirms `resolve_attachment` at stackslib/src/net/atlas/download.rs:1227-1239 never matched, since it used `H'` not `H`).
   - Repeat across `max_attachment_retry_count` re-enqueues (stackslib/src/net/atlas/download.rs:188-205) and assert the batch is eventually dropped without ever fetching real content from a legitimate source.

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

**File:** stackslib/src/net/atlas/download.rs (L186-205)
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

**File:** stackslib/src/net/atlas/download.rs (L434-459)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L1016-1020)
```rust
impl Ord for AttachmentsInventoryRequest {
    fn cmp(&self, other: &AttachmentsInventoryRequest) -> Ordering {
        self.reliability_report.cmp(&other.reliability_report)
    }
}
```

**File:** stackslib/src/net/atlas/download.rs (L1073-1108)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
    }
}

impl Hash for AttachmentRequest {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.content_hash.hash(state)
    }
}

impl Ord for AttachmentRequest {
    fn cmp(&self, other: &AttachmentRequest) -> Ordering {
        other.sources.len().cmp(&self.sources.len()).then_with(|| {
            let (_, report) = self.get_most_reliable_source();
            let (_, other_report) = other.get_most_reliable_source();
            report.cmp(other_report)
        })
    }
}

impl PartialOrd for AttachmentRequest {
    fn partial_cmp(&self, other: &AttachmentRequest) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Requestable for AttachmentRequest {
    fn get_url(&self) -> &UrlString {
        let (url, _) = self.get_most_reliable_source();
        url
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

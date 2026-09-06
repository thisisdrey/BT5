### Title
Missing content-hash verification in `AttachmentsBatchStateContext::extend_with_attachments` lets the sole source peer for a `content_hash` starve retries until batch drop - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`extend_with_attachments` accepts any HTTP 200 response that successfully hex-decodes via `decode_atlas_get_attachment()` and inserts the returned `Attachment` into `context.attachments` without ever comparing `attachment.hash()` against the `content_hash` that was actually requested (`AttachmentRequest.content_hash`). Because the `Done` state handler in `AttachmentsDownloader::run` also indexes and resolves strictly by `attachment.hash()` (the attacker-controlled value), a malicious sole source for a legitimate `content_hash H` can answer every retry with wrong-hash content, so `H` is never resolved and `AttachmentsBatch::bump_retry_count` eventually exceeds `max_attachment_retry_count`, causing the batch to be dropped.

### Finding Description
`AttachmentRequest` is built per `content_hash` with a `sources` map of peers whose gossiped inventory claims to have that hash [1](#0-0) . The request URL sent on the wire is `/v2/attachments/{content_hash}` [2](#0-1) , but nothing in the response handling ties the returned bytes back to that requested hash.

In `AttachmentsBatchStateContext::extend_with_attachments`, the response is decoded and inserted purely based on successful hex-decoding, with no equality check against `request.content_hash`: [3](#0-2) 

`GetAttachmentResponse`'s `Deserialize` impl only hex-decodes the payload into an `Attachment`; it performs no hash validation either [4](#0-3) .

When the state machine reaches `Done`, the downloader looks up matching attachment instances and resolves the batch using the *attacker-supplied* content's own hash (`attachment.hash()`), not the originally requested `H`: [5](#0-4) 

Since the wrong-hash blob's hash almost certainly matches no tracked `AttachmentInstance`, `find_all_attachment_instances` returns empty, no `resolved_attachments` entry for `H` is produced, and `resolve_attachment` is invoked with the wrong hash — leaving `H` still present in `attachments_batch.attachments_instances`. The batch is therefore judged not fully succeeded, `bump_retry_count()` is called, and it is re-enqueued until `retry_count >= max_attachment_retry_count`, at which point it is dropped: [6](#0-5) [7](#0-6) 

If the malicious peer is the sole entry in `AttachmentRequest.sources` for `H` (e.g., it is the only peer whose gossiped inventory bit claims to have it, per `get_prioritized_attachments_requests`), every retry cycle re-selects that same peer as `get_most_reliable_source()` [8](#0-7) , so the attacker can keep answering with wrong-hash content on every attempt, guaranteeing the batch is eventually dropped rather than resolved, even though a legitimately committed attachment for `H` exists network-wide.

### Impact Explanation
This breaks `attachment_available_for(H)` as tracked by the victim node: a genuinely on-chain-committed BNS attachment becomes permanently "not found" (`AtlasDB::find_attachment` never returns it) purely because the sole known source lied about its content, not because the data doesn't exist. Clients resolving BNS names through this node will get incorrect/missing zonefile or attachment data for a name that is validly committed elsewhere on the network — an attachment/BNS mismatch, matching the High severity category.

### Likelihood Explanation
- The attacker only needs to be a normal P2P/RPC-reachable peer that (a) gossips an inventory bit claiming to hold `H`, and (b) is the only peer that does so for that `content_hash` in `context.peers` — a realistic condition for less-popular attachments or a well-timed sybil peer with a good/no track record initially.
- Reachability: the endpoint is a plain HTTP GET (`/v2/attachments/{hash}`) served by any peer's public HTTP interface; no auth, secret, or privileged role is needed.
- Cost: negligible — the attacker sends a small, arbitrary hex payload for each retry, and can repeat indefinitely since `ReliabilityReport::bump_failed_requests()` only affects an unused-in-decision score field and does not block the peer from being selected again as the (sole) source.
- No signature, checksum, or admission-control gate anywhere in this path (`decode_atlas_get_attachment`, `extend_with_attachments`, `Done` handler) blocks the mismatch.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`), mark/deprioritize or blacklist that source for this hash, and — if it was the only source — resolve to a definitive "no valid source" state for that instance rather than silently retrying against the same lying peer forever. Additionally, when re-enqueuing on retry, drop or downrank sources that produced hash-mismatched content so the batch doesn't just keep retrying the same malicious sole source.

### Proof of Concept
Rust test in `stackslib::net::atlas` (extending patterns already in `stackslib/src/net/atlas/tests.rs`, using `new_attachments_batch_from`, `new_peers`, `AttachmentsBatchStateContext`):
1. Build an `AttachmentInstance`/`AttachmentsBatch` for content `H` with a single peer URL in `sources`/`peers`.
2. Simulate `BatchedRequestsResult<AttachmentRequest>.succeeded` containing `(request_for_H, Some(http_response_with_wrong_content))` where the decoded `Attachment`'s hash != `H`, repeated across `max_attachment_retry_count` calls to `AttachmentsBatchStateContext::extend_with_attachments` → `AttachmentsDownloader::run`'s `Done` branch.
3. Assert after each cycle that `context.attachments_batch.attachments_instances` still contains `H` (never resolved) and that `atlasdb.find_attachment(&H)` returns `Ok(None)`.
4. After `max_attachment_retry_count` iterations, assert the batch is no longer present in `priority_queue` (dropped via the "Atlas: dropping batch ... retries count exceeded" branch at `stackslib/src/net/atlas/download.rs:199-204`), while `find_attachment(&H)` still returns `None`, confirming the permanent-non-resolution behavior.

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

**File:** stackslib/src/net/atlas/download.rs (L183-205)
```rust
                for (peer_url, report) in context.peers.drain() {
                    self.reliability_reports.insert(peer_url, report);
                }

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

**File:** stackslib/src/net/atlas/download.rs (L461-472)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L1073-1080)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
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

**File:** stackslib/src/net/atlas/download.rs (L1183-1194)
```rust
    pub fn bump_retry_count(&mut self) {
        self.retry_count += 1;
        let delay = cmp::min(
            MAX_RETRY_DELAY,
            2u64.saturating_pow(self.retry_count as u32).saturating_add(
                thread_rng().gen::<u64>() % 2u64.saturating_pow((self.retry_count - 1) as u32),
            ),
        );

        debug!("Atlas: Re-attempt download in {} seconds", delay);
        self.retry_deadline = get_epoch_time_secs() + delay;
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

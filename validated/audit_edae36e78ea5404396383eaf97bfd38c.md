### Title
Attachment content served by a peer is never checked against the requested `content_hash` before being stored and used to resolve batch state - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateMachine`/`AttachmentsDownloader::run` never verify that the `Attachment` returned in an HTTP response to a `GET /v2/attachments/{content_hash}` request actually hashes to the `content_hash` that was requested. The downloaded bytes' own hash (`attachment.hash()`) is used for storage and for resolving the pending batch entry, so a malicious peer can return arbitrary content and have it accepted, while the actually-requested attachment is never resolved.

### Finding Description
`AttachmentRequest::content_hash` (set in `AttachmentsBatchStateContext::get_prioritized_attachments_requests`, `stackslib/src/net/atlas/download.rs:467-472`) is the hash the client is soliciting. The HTTP GET is built from that hash via `request_path` (`/v2/attachments/{content_hash}`), but the client-side response decoder, `StacksHttpResponse::decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:159-165`), only base64/hex-decodes the JSON body into an `Attachment { content }` — it performs **no comparison** between `Hash160::from_data(&content)` and the `content_hash` that was originally requested.

That decoded `Attachment` is inserted unchecked into `context.attachments` in `extend_with_attachments` (`stackslib/src/net/atlas/download.rs:547-548`). Later, in `AttachmentsDownloader::run`, for each such attachment: [1](#0-0) 
the code computes `attachment.hash()` (the hash of the bytes actually received) and uses it both to look up existing `AttachmentInstance`s (`find_all_attachment_instances`), to persist the blob (`insert_instantiated_attachment`), and to call `context.attachments_batch.resolve_attachment(&attachment.hash())`.

`resolve_attachment` (`stackslib/src/net/atlas/download.rs:1227-1239`) removes batch entries whose *tracked* `content_hash` equals the passed hash — it has no knowledge of which `AttachmentRequest` produced this `Attachment`, so it can only match by coincidence.

Exploit flow: the downloader dispatches an `AttachmentRequest{content_hash: H1}` to a peer (chosen because the peer's inventory claims to have it, see `get_prioritized_attachments_requests`). A malicious peer, instead of returning the real bytes hashing to `H1`, returns a JSON body wrapping arbitrary bytes `b2` (hash `H2 != H1`). The client accepts this uncritically, computes `attachment.hash() == H2`, calls `resolve_attachment(&H2)` which does nothing useful (no batch entry tracked under `H2`, since the tracked entry for that item is keyed by `H1`), and separately stores `b2` under `H2` in `atlasdb` via `insert_instantiated_attachment`. The batch entry for `H1` is left in `attachments_batch.attachments_instances`, unresolved.

Existing guards do not prevent this: there is no signature or hash-equality check anywhere between the HTTP response decode and the batch-resolution call; the only cap is `MAX_ATTACHMENT_SIZE` type constraints elsewhere in the DB layer, which are unrelated to hash-correctness. This confirms the broken equality the question describes: the hash used to resolve batch state is `attachment.hash()` of the returned bytes, not the originally requested `content_hash`.

### Impact Explanation
This is an attachment mismatch (matches the "attachment/BNS mismatch" category): a legitimate attachment referenced from an on-chain `AttachmentInstance` (e.g. a BNS zonefile) never gets fetched from a malicious peer, and the node instead stores an unrelated, attacker-chosen blob keyed by its own (attacker-controlled) hash `H2` in the local `atlasdb`. The real content for `H1` is repeatedly retried (`bump_retry_count`) until `max_attachment_retry_count` is exhausted and the batch is dropped, at which point the node permanently fails to resolve a valid, on-chain-committed attachment (e.g., a BNS name's zonefile), even though the data may be available from honest peers. This is a low-severity availability/mismatch bug rather than a memory-safety or state-corruption bug: no existing/committed batch entry other than the (non-matching) attacker-hash slot is corrupted, since `resolve_attachment` only mutates entries whose tracked hash exactly equals the passed value. It is repeatable per attachment/per malicious peer selection.

### Likelihood Explanation
Requires the attacker to run/control a P2P-reachable peer that is selected as an attachment source — achievable by advertising the target's bit in its `GetAttachmentsInvResponse` inventory (no privileged role needed) and being chosen by the reliability/priority logic in `get_prioritized_attachments_requests`. Attack cost is a single crafted HTTP 200 response with an arbitrary hex payload; no cryptographic secret or admin access is required, matching the unprivileged remote-attacker model. Repeatable each retry cycle.

### Recommendation
In `StacksHttpResponse::decode_atlas_get_attachment` (or immediately after, in `extend_with_attachments`), verify `Hash160::from_data(&response.attachment.content) == request.content_hash` before accepting the attachment into `context.attachments`; reject/treat as failed (`report.bump_failed_requests()`) otherwise. Additionally, thread the originally-requested `content_hash` alongside the downloaded `Attachment` through `AttachmentsBatchStateMachine::Done` so `resolve_attachment` is called with the verified, requested hash rather than the attacker-influenced `attachment.hash()`.

### Proof of Concept
Rust test plan (in `stackslib/src/net/atlas/tests.rs` or a new integration test):
1. Build an `AttachmentsBatch` tracking one `AttachmentInstance` with `content_hash = H1` (via `new_attachment_instance_from`).
2. Build `AttachmentsBatchStateContext` and get `AttachmentRequest{content_hash: H1, ...}` via `get_prioritized_attachments_requests`.
3. Simulate a malicious peer response: construct `Attachment{content: b2}` where `Hash160::from_data(b2) = H2 != H1`, wrap into `GetAttachmentResponse{attachment}`, and insert it into `BatchedRequestsResult::succeeded` for that request (bypassing `decode_atlas_get_attachment`, or feeding raw hex bytes through it to show it does not reject on hash mismatch).
4. Call `context.extend_with_attachments(&mut results)` — assert `context.attachments` contains the attacker's `Attachment` (hash `H2`) despite the request being for `H1`.
5. Simulate `AttachmentsDownloader::run` reaching `Done`: call `context.attachments_batch.resolve_attachment(&attachment.hash())` with `attachment.hash() = H2`.
6. Assert `attachments_batch.attachments_instances_count()` is unchanged (the `H1` entry is still present / not resolved) — i.e., `attachments_batch.has_fully_succeed() == false` — proving the real attachment request for `H1` is never marked resolved, while `insert_instantiated_attachment` would have stored `b2` under `H2` in the atlas DB. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** stackslib/src/net/atlas/download.rs (L1227-1239)
```rust
    pub fn resolve_attachment(&mut self, content_hash: &Hash160) {
        for missing_attachments in self.attachments_instances.values_mut() {
            let mut keys = vec![];
            for (k, hash) in missing_attachments.iter() {
                if hash == content_hash {
                    keys.push(*k);
                }
            }
            for key in keys {
                missing_attachments.remove(&key);
            }
        }
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

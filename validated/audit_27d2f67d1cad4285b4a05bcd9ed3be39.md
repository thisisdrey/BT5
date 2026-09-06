### Title
Missing content-hash verification of downloaded Attachment before storage - (File: stackslib/src/net/atlas/download.rs)

### Summary
`StacksHttpResponse::decode_atlas_get_attachment` in `stackslib/src/net/api/getattachment.rs:158-165` decodes the JSON body into a `GetAttachmentResponse` and returns it without ever comparing the resulting `attachment.hash()` against the `content_hash` of the `AttachmentRequest` that was sent. `AttachmentsBatchStateContext::extend_with_attachments` in `stackslib/src/net/atlas/download.rs:530-558` calls this decode function and, on success, unconditionally does `self.attachments.insert(response.attachment)` — the `request.content_hash` (available in scope, since `request` is the `AttachmentRequest` key of the succeeded map) is never checked.

### Finding Description
The intended security invariant is that a peer answering a `GetAttachment` request should only ever supply content whose SHA/Hash160 digest equals the `content_hash` originally requested (this is the content-addressing contract of Atlas). The code path is:

1. `AttachmentsBatchStateContext::get_prioritized_attachments_requests` builds an `AttachmentRequest { content_hash, sources, .. }` for the specific attachment hash the node is missing (`stackslib/src/net/atlas/download.rs:404-478`).
2. The request is sent to one of the peers claiming (via inventory) to have that attachment.
3. In `extend_with_attachments` (`download.rs:530-558`), for each `(request, response)` pair, the response body is decoded via `response.decode_atlas_get_attachment()` (`getattachment.rs:158-165`), which only performs generic JSON HTTP-payload decoding — it takes no `content_hash` parameter and performs zero comparison against any expected hash.
4. The decoded `Attachment` is inserted directly into `self.attachments` (a `HashSet<Attachment>`) with no `if attachment.hash() != request.content_hash { reject }` check.
5. Back in `AttachmentsDownloader::run` (`download.rs:152-169`), every attachment in `context.attachments.drain()` is looked up via `find_all_attachment_instances(&attachment.hash())` and then unconditionally persisted via `network.atlasdb.insert_instantiated_attachment(&attachment)`, and `context.attachments_batch.resolve_attachment(&attachment.hash())` marks the *original* requested hash as resolved — using the hash of the (unverified) attacker-supplied content, not the original request's `content_hash`.

Because the attachment is keyed/looked up by its own self-reported `attachment.hash()` rather than being checked against `request.content_hash`, a malicious peer can return arbitrary content `C'` for a request for hash `H`. If `C'`'s hash happens to differ from `H` (the normal case, since it's arbitrary attacker content), it still gets inserted into the AtlasDB as an "instantiated attachment" under its own hash `hash(C')`, and `find_all_attachment_instances(&hash(C'))` will find zero instances (since no instance references that bogus hash) — so it wouldn't overwrite the entry for `H`. However, this still allows the attacker to (a) pollute the node's AtlasDB with arbitrary attacker-chosen blobs at zero cost, stored permanently as "instantiated," and (b) since `resolve_attachment` is called with `attachment.hash()` (the attacker-controlled hash) rather than the original request's `content_hash`, if `attachment.hash()` happens to not match any pending instance in the batch, `has_fully_succeed()` for the batch will never become true for the *actually requested* hash `H`, so the batch keeps retrying/never resolves that specific instance — this is at most an availability/pollution issue, not a substitution of canonical BNS-bound data, because the mismatch is filtered out at the "does this attachment_index/content_hash match a known instance" step via `find_all_attachment_instances`.

### Impact Explanation
The lack of a `decoded.hash() == request.content_hash` check means the client accepts and stores whatever an unprivileged remote peer serves for a `GetAttachment` request without validating content-addressing. The concrete effect is unauthenticated write of arbitrary attacker-controlled blobs into the local `AtlasDB.attachments` table (`insert_instantiated_attachment`), which is a real defect. Whether this rises to "attachment/BNS mismatch being served as canonical" depends on whether `find_attachment`/`find_uninstantiated_attachment` lookups elsewhere key strictly by hash (they do — `db.rs` schema has `hash TEXT UNIQUE PRIMARY KEY`), which limits the blast radius to storing junk under the attacker's own chosen hash key rather than substituting content under the *victim's* requested hash. I could not fully verify within the available context whether `insert_instantiated_attachment` itself performs a `Hash160::from_data(&content) == hash` self-consistency check before writing (that function's body was not reached in the exploration budget), which is the remaining open question determining whether this is exploitable as true hash-substitution versus benign self-consistent storage under an arbitrary key.

### Likelihood Explanation
Any remote peer that is included in a `GetAttachmentsInv` response as claiming to hold an attachment, and is then asked for it, can trigger this path with a single crafted HTTP response — no authentication or privileged role is needed, matching the unprivileged-attacker model. Preconditions are minimal: the attacker's node/URL must appear in `network.get_outbound_sync_peers()` and answer an inventory request claiming to have the attachment, which is attacker-controlled data with no verification prior to this fetch.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`), immediately after `response.decode_atlas_get_attachment()` succeeds, compare `response.attachment.hash()` against `request.content_hash` and treat a mismatch as a failed request (`report.bump_failed_requests()` and skip insertion), rather than trusting `attachment.hash()` computed from attacker-supplied bytes as the storage key. Additionally, verify this same equality inside `AtlasDB::insert_instantiated_attachment` as a defense-in-depth measure so hash/content self-consistency is enforced at the storage layer regardless of caller.

### Proof of Concept
Rust test plan (to add to `stackslib/src/net/atlas/tests.rs` or `stackslib/src/net/api/tests/getattachment.rs`):
1. Construct an `AttachmentRequest` with `content_hash = H` (e.g., `Hash160::from_data(b"expected")`).
2. Build a raw HTTP 200 JSON response body of the form `{"attachment":{"content":"<hex of arbitrary bytes b\"evil\">"}}` (i.e., a `GetAttachmentResponse` whose embedded `Attachment` content hashes to `H' != H`).
3. Call `StacksHttpResponse::decode_atlas_get_attachment` on this crafted response and assert it returns `Ok(GetAttachmentResponse { attachment })` with `attachment.hash() != H`, proving zero validation at decode time — matching the exact assertion the question specifies.
4. Optionally, drive this through `AttachmentsBatchStateContext::extend_with_attachments` with a `BatchedRequestsResult` mapping the original `AttachmentRequest{content_hash: H}` to this crafted `Some(response)`, and assert `context.attachments` now contains the attacker's attachment (with hash `H'`) despite the request being for `H`, confirming the missing equality check identified in the question. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

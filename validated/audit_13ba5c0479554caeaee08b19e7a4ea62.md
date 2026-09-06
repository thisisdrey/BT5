Confirmed: `decode_atlas_get_attachment` (stackslib/src/net/api/getattachment.rs:159-165) just parses JSON with no hash check, and `extend_with_attachments` (download.rs:530-558) inserts `response.attachment` into the `HashSet<Attachment>` without ever comparing `attachment.hash()` against `request.content_hash`.### Title
Attachment content is stored without verifying it hashes to the requested `content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` inserts the `attachment` field of a `GetAttachmentResponse` into `self.attachments` after only checking that the HTTP response parses as valid JSON, never comparing `attachment.hash()` to the `AttachmentRequest.content_hash` that was actually requested. A malicious/misbehaving peer selected as a data source can return arbitrary bytes as "the attachment" for any requested hash, and the node will store it in AtlasDB as an instantiated attachment while never resolving the pending `AttachmentInstance` for the real committed hash.

### Finding Description
The claimed equality `attachment.hash() == request.content_hash` is never checked anywhere in the download path. `get_prioritized_attachments_requests` builds an `AttachmentRequest` with a specific `content_hash` and a set of candidate peer `sources` [1](#0-0) . The request is sent via `StacksHttpRequest::new_getattachment` to `GET /v2/attachments/<hash>` [2](#0-1) .

When the response comes back, `extend_with_attachments` decodes it with `decode_atlas_get_attachment` and unconditionally inserts the returned `Attachment` into the `HashSet<Attachment>` — no hash comparison against `request.content_hash` is performed: [3](#0-2) .

`decode_atlas_get_attachment` itself only parses the JSON payload into a `GetAttachmentResponse`; it performs no content-hash validation either: [4](#0-3) .

Later, in `AttachmentsDownloader::run`, the `Done` state iterates the (wrongly-populated) `context.attachments` set and looks up pending instances **by the received attachment's own hash**, not by the originally requested `content_hash`: [5](#0-4) 

Because the lookup key is `attachment.hash()` (computed from the attacker-supplied bytes), it will not match any `AttachmentInstance` queued for the real `content_hash` (unless the attacker performs a hash collision, which is computationally infeasible). The forged attachment is nonetheless persisted via `insert_instantiated_attachment(&attachment)` into the AtlasDB `attachments` table, while `context.attachments_batch.resolve_attachment(&attachment.hash())` also operates on the wrong hash, so the real, on-chain-committed attachment instance for `content_hash` is never resolved and is left pending/missing forever (until retries exhaust and the batch is dropped).

Any peer that a remote/unprivileged node selects as a download source for an `AttachmentRequest` (the peer just needs to be an outbound sync peer providing a data URL and claiming — via inventory gossip — to have the attachment) can serve this forged response. Serving inventory pages that falsely claim to have any attachment is itself unauthenticated and requires no privilege (`extend_with_inventories` similarly stores inventory responses that only need to parse as JSON, without any signature/authentication check) [6](#0-5) .

### Impact Explanation
This lets an unprivileged remote peer:
1. Cause the node to permanently mark a legitimate, on-chain-committed attachment as unresolved/missing, since the correct `content_hash` is never matched against anything after this bug (the AttachmentInstance queued for it stays unresolved and the batch will eventually be dropped after `max_attachment_retry_count` retries).
2. Pollute the node's local AtlasDB `attachments` table with attacker-chosen content that was never committed on-chain by any name operation, stored with `was_instantiated=1`.

This matches the "High — attachment/BNS mismatch" category: a node ends up treating attacker-supplied data as validated attachment content while failing to serve/resolve the actual canonical attachment tied to the real hash committed on-chain. It does not directly forge a name-to-attachment binding served externally over RPC (since `find_attachment` is keyed by hash, and the forged entry is keyed by its own — different — hash, it would only be returned if a future name operation happens to reference that same hash), but it does create a persistent state-integrity issue and a permanent resolution failure/DoS for the correct attachment content per request.

### Likelihood Explanation
The attacker needs only to be selected as one of the `sources` for an `AttachmentRequest` (i.e., is an outbound sync peer with a data URL, and its earlier attachment-inventory response falsely claimed to have the page bit set for the target attachment) — no secret, signature, or privileged role is required, since inventory and attachment-serving are unauthenticated RPC/P2P interactions. The cost is a single crafted HTTP response per targeted `content_hash`, and the effect is repeatable for every attachment the node tries to fetch, as long as the attacker keeps being chosen as a source.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558), after decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`), and optionally flag the peer as faulty/deregister the event, matching the surrounding pattern already used for decode failures.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/download.rs` (or a new test module reusing its types):
1. Construct an `AttachmentsBatchStateContext` via `AttachmentsBatchStateContext::new` with a single peer URL and empty `ConnectionOptions`.
2. Build an `AttachmentRequest` with `content_hash = Hash160::from_data(b"right")` and `sources` containing that peer.
3. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has that request mapped to `Some(StacksHttpResponse)` constructed so that `decode_atlas_get_attachment` yields `GetAttachmentResponse { attachment: Attachment { content: b"wrong".to_vec() } }`.
4. Call `context.extend_with_attachments(&mut results)` and assert:
   - `context.attachments` contains an `Attachment` whose `.hash()` equals `Hash160::from_data(b"wrong")`.
   - `context.attachments` does **not** contain any attachment whose `.hash()` equals `Hash160::from_data(b"right")`.
5. Feed this context through the `Done` arm logic in `AttachmentsDownloader::run` (or replicate its loop) against a `PeerNetwork`/`AtlasDB` that has a queued `AttachmentInstance` for `content_hash = Hash160::from_data(b"right")`, and assert:
   - `network.atlasdb.find_all_attachment_instances(&Hash160::from_data(b"right"))` still returns the pending instance (unresolved).
   - `network.atlasdb.find_attachment(&Hash160::from_data(b"wrong"))` returns `Some(Attachment{content: b"wrong"})` with `was_instantiated = 1`, proving the forged content was persisted while the real requested hash remains unresolved.

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

**File:** stackslib/src/net/atlas/download.rs (L466-474)
```rust
                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
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

**File:** stackslib/src/net/api/getattachment.rs (L145-155)
```rust
impl StacksHttpRequest {
    /// Make a new request for an attachment
    pub fn new_getattachment(host: PeerHost, attachment_id: Hash160) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            host,
            "GET".into(),
            format!("/v2/attachments/{}", &attachment_id),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to construct request from infallible data")
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

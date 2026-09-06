Verified: the described gap is real in the code as written.

### Title
Unbounded, hash-unverified attachment content is decoded and unconditionally persisted to AtlasDB - (File: stackslib/src/net/atlas/download.rs, stackslib/src/net/atlas/mod.rs)

### Summary
`StacksHttpResponse::decode_atlas_get_attachment` parses the JSON body into `GetAttachmentResponse` purely via hex-decoding, with no length cap and no check that `Hash160::from_data(&attachment.content)` matches the `content_hash` that was requested in `/v2/attachments/{content_hash}`. The result is fed straight into `AttachmentsBatchStateContext::extend_with_attachments`, which stores the attachment in a `HashSet` and later unconditionally calls `AtlasDB::insert_instantiated_attachment` regardless of whether the content actually corresponds to any known instance.

### Finding Description
`GetAttachmentResponse::deserialize` only does `hex_bytes(&hex_encoded)` into `Attachment::new(bytes)` with no size assertion: [1](#0-0) 

`StacksHttpResponse::decode_atlas_get_attachment` just JSON-deserializes the body into that struct and returns it — no comparison against the requested hash and no bound check: [2](#0-1) 

The request path only carries the *expected* hash (`content_hash`) as a `Requestable` key, not as a value checked post-decode: [3](#0-2) 

`extend_with_attachments` takes `decode_atlas_get_attachment()`'s result and inserts `response.attachment` into `self.attachments` with **no comparison of `response.attachment.hash()` to `request.content_hash`**: [4](#0-3) 

In `AttachmentsDownloader::run`, every attachment drained from that set is written to the database via `insert_instantiated_attachment` **unconditionally**, independent of whether `find_all_attachment_instances(&attachment.hash())` found any matching pending instance: [5](#0-4) 

`AtlasConfig.attachments_max_size` (default `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) is only used for config-file validation of the configured value itself; it is never consulted when decoding or storing a downloaded attachment body: [6](#0-5) [7](#0-6) 

Root cause: the download/storage path trusts peer-supplied `attachment.content` both for size and for identity (no `Hash160::from_data(content) == request.content_hash` equality check anywhere between decode and DB write).

### Impact Explanation
Any peer selected from `network.get_outbound_sync_peers()` to serve an attachment request (an attacker can trivially become an outbound sync peer of a victim by running their own node — no privileged role, secret, or key is required) can, upon receiving a legitimate `/v2/attachments/{hash}` request, respond with a body containing an arbitrarily large hex-encoded `content` field. That content is decoded with no size cap, inserted into an in-memory set, and then unconditionally written to the node's local AtlasDB via `insert_instantiated_attachment`, regardless of whether it matches the hash that was actually requested. This is a repeatable, unauthenticated write of attacker-controlled, unbounded-size data into persistent state (`insert_instantiated_attachment`) for every attachment fetch the node performs, enabling storage exhaustion. Because content identity is never checked against the requested hash, the write is also not tied to the on-chain BNS commitment that triggered the download in the first place — the node persists data that no canonical block/attachment instance validated.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to run a normal Stacks peer, be selected as an outbound Atlas-sync source (a normal, unprivileged state reached simply by peering and advertising the relevant BNS-page inventory), and answer a `GET /v2/attachments/{hash}` request the victim node itself issues. No secret, signature, or admin capability is required, and the behavior is fully repeatable on every attachment batch the node processes, at attacker's cost of running a single peer.

### Recommendation
In `StacksHttpResponse::decode_atlas_get_attachment` (or immediately in `extend_with_attachments`), enforce: (1) `resp.attachment.content.len() <= connection_options`/`AtlasConfig.attachments_max_size` before accepting the response, rejecting/treating as a faulty peer otherwise; (2) `Hash160::from_data(&resp.attachment.content) == request.content_hash` before inserting into `self.attachments`, discarding mismatches and bumping `report.bump_failed_requests()`. Also guard `insert_instantiated_attachment` in `AttachmentsDownloader::run` to only persist attachments whose hash matches at least one tracked `AttachmentInstance`.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/tests.rs` or `stackslib/src/net/api/tests/getattachment.rs`:
1. Construct a `StacksHttpResponse` (200 OK, JSON body) whose body is `to_hex(&vec![0u8; 8 * 1024 * 1024])` wrapped as the `GetAttachmentResponse` hex string (per `GetAttachmentResponse::serialize`), i.e., simulate a malicious peer's raw HTTP response bytes for a request to `/v2/attachments/<some_hash>`.
2. Call `response.decode_atlas_get_attachment()` and assert it returns `Ok(GetAttachmentResponse { attachment })` with `attachment.content.len() == 8*1024*1024`, with no error despite exceeding `ATTACHMENTS_MAX_SIZE_MIN` (1_048_576).
3. Additionally assert `attachment.hash() != <some_hash>` (the originally requested hash) to demonstrate no equality enforcement exists.
4. Optionally drive `AttachmentsBatchStateContext::extend_with_attachments` with a `BatchedRequestsResult` containing this response for an `AttachmentRequest{content_hash: <some_hash>, ...}` and confirm `context.attachments` now contains the oversized, hash-mismatched `Attachment`, proving it would reach `AtlasDB::insert_instantiated_attachment` unfiltered.

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
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

**File:** stackslib/src/net/atlas/mod.rs (L116-122)
```rust
    pub fn validate(&self) -> Result<(), String> {
        if self.attachments_max_size < ATTACHMENTS_MAX_SIZE_MIN {
            Err(format!(
                "Invalid value for `attachments_max_size`: {}. Expected {} or greater",
                self.attachments_max_size, ATTACHMENTS_MAX_SIZE_MIN
            ))
        } else if self.max_uninstantiated_attachments < MAX_UNINSTANTIATED_ATTACHMENTS_MIN {
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

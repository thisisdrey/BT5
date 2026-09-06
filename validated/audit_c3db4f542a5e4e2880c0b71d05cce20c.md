### Title
Missing content-hash verification on peer `GetAttachmentResponse` lets a malicious peer permanently prevent resolution of a legitimately committed attachment - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `Attachment` returned by `decode_atlas_get_attachment` without checking that `Hash160::from_data(&attachment.content)` equals the `content_hash` that was actually requested (`AttachmentRequest.content_hash`). A remote peer can answer a request for a real, pending `content_hash` `H` with an `Attachment{content: vec![]}` (or any other wrong bytes); this decodes successfully, is inserted keyed by its own hash (`Hash160::from_data(&[])`, which is a fixed value distinct from the `Hash160::empty()` sentinel used elsewhere), and is never matched to `H` by `find_all_attachment_instances`/`resolve_attachment`. The genuinely committed attachment instance for `H` is left unresolved, retried, and eventually dropped when `max_attachment_retry_count` is exhausted, while the peer's malicious response is even scored as a "successful" request.

### Finding Description
The download pipeline is: `AttachmentsDownloader::run` → `AttachmentsBatchStateMachine::try_proceed` → `extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-559) processes HTTP responses to `AttachmentRequest`s: [1](#0-0) 

`response.decode_atlas_get_attachment()` (stackslib/src/net/api/getattachment.rs:159-165) simply JSON-decodes the body into `GetAttachmentResponse` with zero verification that the returned `attachment.content` actually hashes to the `content_hash` that was requested (that value, `request.content_hash`, is available on the `AttachmentRequest` but is discarded here): [2](#0-1) 

`GetAttachmentResponse`'s `Deserialize` impl merely hex-decodes an arbitrary string into `Attachment.content` with no hash check either: [3](#0-2) 

The decoded `Attachment` is inserted into `self.attachments: HashSet<Attachment>` keyed by nothing but its own bytes, and the peer's report is marked as a successful request — even though the content is wrong: [4](#0-3) 

When the FSM reaches `Done`, each collected attachment is resolved by *its own* hash, not by the hash that was originally requested: [5](#0-4) 

For an attacker-supplied empty content, `attachment.hash()` is `Hash160::from_data(&[])` — a fixed value distinct from `Hash160::empty()` (all-zero sentinel used only for "no attachment" instances in `check_attachment_instances`): [6](#0-5) [7](#0-6) 

Since `find_all_attachment_instances(&attachment.hash())` (stackslib/src/net/atlas/db.rs:630-639) queries by `content_hash`, it will not find the instance whose real `content_hash` is `H` (unless `H` happened to equal the hash of empty content by pure coincidence). Consequently `context.attachments_batch.resolve_attachment(&attachment.hash())` (stackslib/src/net/atlas/download.rs:1227-1239) removes nothing for `H`, so `has_fully_succeed()` stays false, the batch is retried, and after `retry_count >= max_attachment_retry_count` it is silently dropped: [8](#0-7) 

The root cause is the broken equality the question describes: "attachment marked resolved" should imply "the instance's committed `content_hash` was actually matched," but nothing in `extend_with_attachments` or the `Done` handling verifies `Hash160::from_data(&attachment.content) == request.content_hash` before trusting/storing the response. Existing guards (`Hash160::empty()` check in `check_attachment_instances`, `find_attachment`/`find_uninstantiated_attachment` lookups) only address instances whose commitment is explicitly the zero sentinel or that already exist locally; they do nothing to validate peer-supplied bytes against the specific hash that was requested.

### Impact Explanation
A remote, unprivileged peer that is (or claims via a forged `GetAttachmentsInvResponse` to be) a source for a pending attachment can serve wrong/empty content for `/v2/attachments/{H}` and thereby cause the legitimately committed attachment for `H` to never resolve: it is retried until `max_attachment_retry_count` is exhausted and then permanently dropped from the node's Atlas state (BNS zonefile data, in practice). This matches the listed High-severity category "attachment/BNS mismatch" — a legitimately committed attachment is effectively denied to the node, and the attacker's bogus response is scored as a "successful" request, which increases that peer's `ReliabilityReport` and makes it more likely to be selected as the preferred source in future rounds (`get_most_reliable_source`), amplifying the effect over time. This is repeatable per attachment request and requires only responding on the RPC port with a well-formed JSON body.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to run a normal peer/RPC endpoint reachable by the victim node and be selected as (one of) the sources for a pending attachment's inventory — trivially achievable since `GetAttachmentsInvResponse` bits are also unauthenticated and not cross-checked against actual possession of content. No secret, admin role, or privileged position is required. The attacker's cost is a single crafted HTTP 200 response per request; the effect compounds because bogus "successes" raise the attacker's reliability score, increasing future selection probability.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-559), after `decode_atlas_get_attachment()` succeeds, verify `Hash160::from_data(&response.attachment.content) == request.content_hash` before inserting into `self.attachments` and before calling `report.bump_successful_requests()`. If the hash doesn't match, treat it as a failed/faulty response (`bump_failed_requests()`, and consider treating the peer as faulty for repeated mismatches) so the real `content_hash` remains queued for other sources instead of being silently swallowed under the wrong key.

### Proof of Concept
Rust test plan (to live alongside `stackslib/src/net/atlas/tests.rs`):
1. Build a real `AttachmentInstance` with a genuine non-empty `content_hash = H` (e.g., via `new_attachment_instance_from`) and construct an `AttachmentsBatch`/`AttachmentsBatchStateContext` tracking it, with one peer source.
2. Simulate `get_prioritized_attachments_requests()` producing an `AttachmentRequest{content_hash: H, ...}`.
3. Craft a `BatchedRequestsResult` where the "succeeded" response for that request is a `StacksHttpResponse` whose body is `GetAttachmentResponse{attachment: Attachment{content: vec![]}}` (serialized as `"0x"`/empty hex, matching the real `GetAttachmentResponse` `Serialize` impl).
4. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments` contains `Attachment::empty()` while `context.attachments_batch.attachments_instances` still contains an entry with hash `H` (unresolved) after the equivalent of the `Done` processing block (`resolve_attachment(&Attachment::empty().hash())` does not clear `H`).
5. Drive `AttachmentsDownloader::run` in a loop across `max_attachment_retry_count` iterations, feeding the same malformed response each retry (or a mocked `PeerNetwork`/`AtlasDB`), then assert `attachments_batch.retry_count == max_attachment_retry_count` and the batch is dropped (not re-pushed to `priority_queue`), and `atlasdb.count_unresolved_attachment_instances()` / `find_all_attachment_instances(&H)` shows the instance for `H` never became available (`is_available == 0`), proving the legitimately committed attachment was permanently lost due to the unverified empty-content response.

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

**File:** stackslib/src/net/atlas/download.rs (L540-552)
```rust
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

**File:** stackslib/src/net/atlas/mod.rs (L62-77)
```rust
impl Serialize for GetAttachmentResponse {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let hex_encoded = to_hex(&self.attachment.content[..]);
        s.serialize_str(hex_encoded.as_str())
    }
}

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

**File:** stackslib/src/net/atlas/mod.rs (L153-165)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
    }

    pub fn hash(&self) -> Hash160 {
        Hash160::from_data(&self.content)
    }

    pub fn empty() -> Attachment {
        Attachment { content: vec![] }
    }
}
```

**File:** stacks-common/src/util/hash.rs (L212-216)
```rust
impl MerkleHashFunc for Hash160 {
    fn empty() -> Hash160 {
        Hash160([0u8; 20])
    }

```

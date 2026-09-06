### Title
Unbounded storage of unauthenticated attachment content via `AttachmentsDownloader::run` — stored attachments not verified against requested `content_hash` and never evicted - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
When a Stacks node requests an attachment (`GET /v2/attachments/<hash>`) from an outbound peer to resolve a queued `AttachmentInstance`, the response is decoded via `decode_atlas_get_attachment` and unconditionally passed to `atlasdb.insert_instantiated_attachment(&attachment)` without ever checking that the returned content's hash equals the `content_hash` that was actually requested. A malicious peer can therefore answer any legitimate attachment request with arbitrary, maximal-size content, which is permanently stored as an "instantiated" attachment — a class of record that, unlike uninstantiated attachments, has no eviction routine in this codebase.

### Finding Description
The broken equality is: `attachment.hash() == <the content_hash of the AttachmentInstance that triggered the request>`. This equality is never checked.

Trace:
1. `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`) iterates `results.succeeded`, calls `response.decode_atlas_get_attachment()` and does `self.attachments.insert(response.attachment)` — the only "validation" performed is that the JSON parses; no comparison to the `AttachmentRequest`'s target hash is made. [1](#0-0) 
2. `decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:159-165`) simply hex-decodes whatever the peer sent as a string and wraps it in `Attachment` — the only limit is the outer HTTP body/message-length cap, not any field specific to attachment content or its relationship to the requested hash. [2](#0-1) 
3. `AttachmentsDownloader::run` (`stackslib/src/net/atlas/download.rs:152-169`) then, for every attachment collected in the `Done` state, computes `attachment.hash()` (the hash of the *returned* bytes, not the hash that was originally requested) and unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)`, storing it regardless of whether it matches any real `AttachmentInstance`. [3](#0-2) 
4. Only `find_all_attachment_instances(&attachment.hash())` is used to see if the (attacker-controlled) hash happens to match a pending instance for linking purposes — a mismatch does not prevent storage, it only prevents the attachment from being paired/resolved to an instance.
5. Eviction after each batch only targets **uninstantiated** and unresolved-instance records: [4](#0-3) 
There is no equivalent eviction for `insert_instantiated_attachment` records, so bogus content stored this way persists indefinitely.

Attacker's exact message: operate (or be selected as) an outbound Atlas sync peer; when the victim node sends `GET /v2/attachments/<expected_hash>` to resolve a legitimately queued `AttachmentInstance` (created from real, confirmed on-chain zonefile/BNS operations — this precondition is out of scope to forge but occurs naturally during normal chain activity), respond with a 200 JSON body containing an arbitrary hex string near the node's configured `attachments_max_size` / HTTP body cap instead of the correct content. This response is accepted, hex-decoded, and stored via `insert_instantiated_attachment`, with no check that its hash matches the hash the victim actually asked for.

Existing guards that fail to catch this: the general HTTP body cap only bounds a single response's size; `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` bounds the inventory page requests, not attachment content; `attachments_max_size` in `AtlasConfig` is a configured value but was not found enforced at the point of storing a freshly-downloaded attachment in `download.rs`'s `run()`/`extend_with_attachments` path.

### Impact Explanation
A remote peer that a victim node has chosen as an outbound sync peer can cause the victim to durably store arbitrary attacker-chosen content (up to the size cap) under `insert_instantiated_attachment` for every legitimate attachment request the victim issues to that peer, with no verification that the content corresponds to the hash actually requested. Since instantiated attachments have no observed eviction path (unlike uninstantiated ones), this is a repeatable, cumulative disk-exhaustion vector directly tied to how many attachment-fetch attempts the victim naturally performs against the malicious peer — matching the "attachment/BNS mismatch" / bounded-but-cumulative storage-exhaustion category (High).

### Likelihood Explanation
Preconditions: the attacker must be an outbound Atlas sync peer of the victim (achievable by running an ordinary, unprivileged peer and being selected via `network.get_outbound_sync_peers()`), and the victim must have legitimate, chain-driven `AttachmentInstance` records to resolve (normal BNS/zonefile activity, not attacker-controlled). No secret, StackerDB slot ownership, or admin role is required — only normal peer connectivity. The attack is repeatable for every attachment request the victim sends to the malicious peer, at low attacker cost (serve one crafted HTTP response per request).

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or in `AttachmentsDownloader::run` before calling `insert_instantiated_attachment`), verify `attachment.hash() == request.content_hash` (the hash associated with the specific `AttachmentRequest` that produced this response) before accepting/storing the attachment; discard and penalize the peer's reliability score on mismatch. Additionally, enforce `attachments_max_size` explicitly on the decoded `attachment.content.len()` at the point of insertion, and add eviction/quota handling for instantiated attachments that never got linked to a real `AttachmentInstance`.

### Proof of Concept
Rust net test plan (in `stackslib/src/net/atlas/tests.rs` or a new test module):
1. Set up two `AtlasDB`s: victim and attacker-controlled mock HTTP server implementing `RPCGetAttachmentRequestHandler`'s route.
2. Seed the victim's `AtlasDB` with one queued `AttachmentInstance` referencing `content_hash = H` (simulating a resolved on-chain zonefile commitment) so that `AttachmentsDownloader` will issue a `GET /v2/attachments/<H>` request.
3. Have the mock peer respond with a `GetAttachmentResponse` whose `attachment.content` is a large (e.g., `attachments_max_size - 1` bytes) buffer of arbitrary bytes, so `attachment.hash() != H`.
4. Drive `AttachmentsDownloader::run` to completion and then call `atlas_db.find_all_attachment_instances(&attacker_attachment.hash())` — assert it returns empty (no legitimate link), yet assert the row exists via a raw query against the `attachment_instantiated` (or equivalent) table, showing the bogus content was persisted anyway.
5. Repeat with distinct large payloads for multiple requested `content_hash`es and assert monotonic growth of `AtlasDB` storage with no corresponding call to any eviction function removing these rows, demonstrating unbounded accumulation of unverified content.

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

**File:** stackslib/src/net/atlas/download.rs (L174-180)
```rust
                // Every once in a while, we delete uninstantiated attachments
                network.atlasdb.evict_expired_uninstantiated_attachments()?;

                // Every once in a while, we delete outdated, unresolved attachments instances
                network
                    .atlasdb
                    .evict_expired_unresolved_attachment_instances()?;
```

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
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

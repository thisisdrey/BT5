### Title
Unbounded attachment size in `GetAttachmentResponse` deserialization bypasses `attachments_max_size` cap - (File: stackslib/src/net/atlas/mod.rs)

### Summary
`GetAttachmentResponse`'s custom `Deserialize` impl decodes an arbitrary-length hex string into `Attachment::new(bytes)` with no comparison against `AtlasConfig::attachments_max_size`, and the download path in `stackslib/src/net/atlas/download.rs::extend_with_attachments` accepts and stores the decoded attachment into the in-memory `HashSet<Attachment>` without ever calling a size-check like `should_keep_attachment`. This lets any peer serving an `AttachmentRequest` return an oversized payload that gets fully allocated and held in the P2P thread.

### Finding Description
`GetAttachmentResponse::deserialize` at `stackslib/src/net/atlas/mod.rs:69-76` reads a hex string of unconstrained length via `hex_bytes(&hex_encoded)` and builds `Attachment::new(bytes)` immediately - there is no length check against `AtlasConfig.attachments_max_size` (default `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576` bytes) at `stackslib/src/net/atlas/mod.rs:52,107`.

On the download path, `StacksHttpResponse::decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:158-165`) calls this same deserialize logic through `serde_json::from_value`. That function is invoked from `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`), which on success does `self.attachments.insert(response.attachment)` directly - no call to `AtlasDB::should_keep_attachment` or any size gate exists on this path. The size check only happens later, if at all, when attachments are persisted (`insert_instantiated_attachment` in `AttachmentsDownloader::run`, `stackslib/src/net/atlas/download.rs:161`), by which point the oversized `Vec<u8>` has already been fully allocated in memory during JSON/hex decode and held in the `HashSet<Attachment>` in the P2P thread's state machine.

An attacker who volunteers as (or is selected as) a download source for an `AttachmentRequest` (any peer advertising the attachment in its `GetAttachmentsInvResponse` inventory) can respond to the resulting `GET /v2/attachments/<hash>` request with a `GetAttachmentResponse` JSON body encoding a multi-hundred-megabyte (or larger) hex string. Because the underlying HTTP response body/JSON parsing (`parse_json`, `get_http_payload_ok`) does not appear to impose a size bound tied to `attachments_max_size` before this deserialize routine runs, the full payload is decoded and allocated.

### Impact Explanation
A malicious download-source peer can force a victim node's P2P/Atlas-downloader thread to allocate a large `Vec<u8>` (bounded only by the HTTP layer's generic body-size limits, if any) per attachment response, well above the intended `attachments_max_size` cap. This is a bounded compute/memory DoS on the Atlas attachment-download path, repeatable each time the node fetches an attachment instance and selects the attacker as source, matching the "bounded compute DoS on a read/fetch path" category. It does not corrupt consensus state or forge canonical data, since the attachment is not treated as canonical until later checks (if any) at insertion time.

### Likelihood Explanation
Preconditions: the attacker must be selected as a data-URL source for at least one `AttachmentRequest` batch, which requires only that their peer advertises the relevant page in an `AttachmentsInv` response - within reach of any unprivileged peer that participates in outbound Atlas sync. No secrets, keys, or privileged roles are needed; cost to the attacker is minimal (one crafted HTTP response per request). Repeatable across every future attachment fetch cycle where the node queries that peer.

### Recommendation
Enforce `attachments_max_size` (or a hard protocol-level cap) at decode time in `GetAttachmentResponse::deserialize` in `stackslib/src/net/atlas/mod.rs`, rejecting hex payloads whose decoded length exceeds the configured bound before allocating `Attachment::new(bytes)`. Additionally, add an explicit size check in `extend_with_attachments` (`stackslib/src/net/atlas/download.rs`) before inserting into `self.attachments`, mirroring `AtlasDB::should_keep_attachment`.

### Proof of Concept
Rust test in `stackslib::net::atlas` or `stackslib::net::api::getattachment`:
1. Construct a JSON string body: `"\"" + "00".repeat(N) + "\""` with N = e.g. 200_000_000 (200MB decoded).
2. Call `GetAttachmentResponse::deserialize` (via `serde_json::from_str::<GetAttachmentResponse>(&body)`) or `StacksHttpResponse::decode_atlas_get_attachment` on a crafted `StacksHttpResponse` with this body.
3. Assert the call succeeds and `resp.attachment.content.len() == N`, exceeding `ATTACHMENTS_MAX_SIZE_MIN` (`stackslib/src/net/atlas/mod.rs:52`) with no error returned - demonstrating the missing bound check at `stackslib/src/net/atlas/mod.rs:69-76`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
```

**File:** stackslib/src/net/atlas/mod.rs (L69-76)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L107-107)
```rust
            attachments_max_size: ATTACHMENTS_MAX_SIZE_MIN,
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

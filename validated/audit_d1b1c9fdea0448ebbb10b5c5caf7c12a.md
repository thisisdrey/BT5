### Title
Missing `attachments_max_size` enforcement for Atlas-downloaded attachments allows storage-exhaustion via oversized peer response - (File: stackslib/src/net/atlas/download.rs)

### Summary
`GetAttachmentResponse`'s custom `Deserialize` hex-decodes an attacker-controlled JSON string with no length bound, and the resulting `Attachment` is inserted into the downloader's working set in `extend_with_attachments` with no size check at all. The only size gate, `AtlasDB::should_keep_attachment`, is invoked exclusively from the POST-relay ingestion path, not from the Atlas peer-download path, so a malicious Atlas peer can supply an oversized attachment that bypasses `attachments_max_size` entirely.

### Finding Description
`GetAttachmentResponse::deserialize` does `String::deserialize` then `hex_bytes(&hex_encoded)` and wraps the result in `Attachment::new(bytes)` with no upper bound check [1](#0-0) . This type is produced by `StacksHttpResponse::decode_atlas_get_attachment`, which is called when this node, acting as an Atlas-attachment fetcher, parses the HTTP response body from a peer it queried for `/v2/attachments/{hash}` [2](#0-1) .

The downloader consumes this response in `extend_with_attachments`, which unconditionally does `self.attachments.insert(response.attachment)` on any successfully decoded response — there is no check of `attachment.content.len()` against `AtlasConfig.attachments_max_size` anywhere in this function [3](#0-2) .

The only place `attachments_max_size` is enforced is `AtlasDB::should_keep_attachment`, which checks `attachment.content.len() as u32 > self.atlas_config.attachments_max_size` and is explicitly labeled for "posted attachment" (i.e., the POST-relay ingestion path), not the Atlas download path [4](#0-3) . Since `insert_instantiated_attachment` is reachable from the download path via `extend_with_attachments` without ever routing through `should_keep_attachment`, an oversized attachment fetched from a malicious peer can be persisted with `content.len()` far exceeding the configured maximum.

A remote attacker only needs to: (1) run a peer node, (2) gossip an attachment inventory claiming to have a particular content hash (this requires no privileged role, any peer can advertise inventory), and (3) when the victim node queries `GET /v2/attachments/{hash}` from that peer as part of legitimate attachment-fetching, respond with a JSON body containing an arbitrarily large hex string. The victim node's HTTP client decodes this into an oversized `Attachment` and stores it via the downloader pipeline with no size gate.

### Impact Explanation
This allows a single malicious peer to cause the victim node to persist attachments larger than the operator-configured `attachments_max_size` into its Atlas attachment store, defeating the intended storage cap for this ingestion path. Repeated for many distinct (fabricated) content hashes, this results in unbounded disk usage growth from the download path specifically, which the config's size limit was designed to prevent. This is a size-limit-bypass / storage-exhaustion issue confined to the attachment downloader ingestion path.

### Likelihood Explanation
Low precondition cost: the attacker just needs to run an ordinary Atlas peer, advertise inventory entries for hashes the victim is trying to resolve, and serve arbitrarily large fabricated content when queried. No secret, no privileged role, and no local access are required — this fits the "unprivileged remote peer" threat model. The exploit is repeatable per attachment request cycle.

### Recommendation
Add an `attachment.content.len() as u32 > atlas_config.attachments_max_size` check in `extend_with_attachments` (or inside `decode_atlas_get_attachment`/`GetAttachmentResponse::deserialize`) before inserting into `self.attachments`, mirroring the check already performed in `AtlasDB::should_keep_attachment`, so the size cap applies uniformly to both the POST-relay and Atlas-download ingestion paths.

### Proof of Concept
Construct a `StacksHttpResponse` whose JSON body is `to_hex(&vec![0u8; attachments_max_size as usize + 1])`-equivalent (i.e., a hex string decoding to more bytes than `AtlasConfig.attachments_max_size`). Call `.decode_atlas_get_attachment()` on it (per `stackslib/src/net/api/getattachment.rs:159-165`) to obtain a `GetAttachmentResponse` with an oversized `Attachment`. Feed this as a "succeeded" result into `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-553`) and assert that `self.attachments` contains an entry with `content.len() > atlas_config.attachments_max_size`, demonstrating that no size check rejects it, in contrast to `AtlasDB::should_keep_attachment` (`stackslib/src/net/atlas/db.rs:249-266`) which would reject the same payload on the POST path.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L530-553)
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
```

**File:** stackslib/src/net/atlas/db.rs (L249-266)
```rust
    pub fn should_keep_attachment(
        &self,
        contract_id: &QualifiedContractIdentifier,
        attachment: &Attachment,
    ) -> bool {
        if !self.atlas_config.contracts.contains(contract_id) {
            info!(
                "Atlas: will discard posted attachment - {} not in supported contracts",
                contract_id
            );
            return false;
        }
        if attachment.content.len() as u32 > self.atlas_config.attachments_max_size {
            info!("Atlas: will discard posted attachment - attachment too large");
            return false;
        }
        true
    }
```

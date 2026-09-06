### Title
Missing `attachments_max_size` enforcement before decoding attachment content in `GetAttachmentResponse::deserialize` / `decode_atlas_get_attachment()` - (File: stackslib/src/net/atlas/mod.rs, stackslib/src/net/api/getattachment.rs)

### Summary
When `AttachmentsBatchStateMachine` downloads an attachment via `decode_atlas_get_attachment()`, the response body is JSON-decoded and the hex-encoded content is fully `hex_bytes`-decoded into a `Vec<u8>` with no check against `AtlasConfig.attachments_max_size` before allocation. The size guard (`should_keep_attachment`) exists only on the write/ingest path in `atlas/db.rs`, not on this download/deserialize path.

### Finding Description
`StacksHttpResponse::decode_atlas_get_attachment()` calls `get_http_payload_ok()`, converts to `serde_json::Value`, then `serde_json::from_value::<GetAttachmentResponse>()`, which invokes the custom `Deserialize` impl: [1](#0-0) 

That impl reads the payload string and calls `hex_bytes(&hex_encoded)` to fully allocate the decoded content into an `Attachment`, with no comparison against `attachments_max_size` anywhere in this path: [2](#0-1) 

The result feeds directly into `AttachmentsBatchStateContext::extend_with_attachments`, which inserts the attachment into `self.attachments` on any successful decode: [3](#0-2) 

By contrast, the size check that does exist (`should_keep_attachment`) is only applied on the locally-posted/write path, not the peer-download path: [4](#0-3) 

`ATTACHMENTS_MAX_SIZE_MIN` is 1 MiB by default: [5](#0-4) 

so a malicious peer answering an `AttachmentRequest` for a hash it volunteered to serve can return a JSON body whose hex string decodes to far more than the configured `attachments_max_size`, and the node will allocate that entire buffer before any size check occurs.

### Impact Explanation
The intended resource bound (`attachments_max_size`) is bypassed on the attachment-download path: the requesting node allocates memory proportional to whatever size the malicious peer declares in its response, not the configured cap. This is repeatable per request and can be amplified by batching multiple oversized attachment requests to the same or different malicious peers. I was not able to fully confirm within this investigation whether the generic HTTP/response-body reading layer in `stackslib/src/net/httpcore.rs` (which imports `MAX_MESSAGE_LEN`) imposes an independent hard ceiling on HTTP response body size before this JSON parsing occurs; if such a generic cap exists and is enforced for HTTP responses, the practical allocation is bounded by that value rather than being truly unbounded, which would limit this to a bounded memory/compute cost issue rather than an unbounded-OOM crash primitive. Given this uncertainty, the demonstrable, code-confirmed defect is the bypass of the attachment-specific size guard on the download path — a bounded resource-exhaustion/memory-bloat issue on the Atlas attachment sync path, not a demonstrated unauthenticated crash from a single message.

### Likelihood Explanation
Any peer that is chosen as a source for an `AttachmentRequest` (which requires only that it previously gossiped/claimed to have the attachment inventory) can trigger this by returning an oversized response — no privileged role, secret, or signature is required, matching the "any peer answering the AttachmentRequest" precondition.

### Recommendation
Enforce `attachments_max_size` before allocating the decoded content: check the JSON string's expected decoded length (or the raw HTTP `Content-Length` / hex-string length) against `AtlasConfig.attachments_max_size` prior to calling `hex_bytes` in `GetAttachmentResponse::deserialize`, and/or add an explicit length check in `decode_atlas_get_attachment()` before allocating, mirroring the check already done in `AtlasDB::should_keep_attachment`.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` or `stackslib/src/net/api/tests/getattachment.rs` that constructs a `StacksHttpResponse` whose JSON body is a hex string decoding to significantly more bytes than `AtlasConfig::new(false).attachments_max_size` (e.g. several times `ATTACHMENTS_MAX_SIZE_MIN`), then calls `response.decode_atlas_get_attachment()` and asserts it returns an error/rejection rather than succeeding with an oversized `Attachment.content`. Currently no such check exists, so the call would succeed and allocate the full buffer — confirming the missing guard at `stackslib/src/net/atlas/mod.rs:69-77`.

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

**File:** stackslib/src/net/atlas/download.rs (L546-552)
```rust

            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
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

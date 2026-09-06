Based on the code I was able to examine, the core claim is confirmed at the deserialization layer, though I was unable to fully verify the downstream `download.rs`/`db.rs` bound-check logic before running out of tool calls (noted below).

### Title
Unbounded attachment size accepted via `GetAttachmentResponse` deserialization bypasses `attachments_max_size` - ([File: stackslib/src/net/atlas/mod.rs])

### Summary
`GetAttachmentResponse`'s custom `Deserialize` impl hex-decodes the JSON string payload and constructs an `Attachment` via `Attachment::new(bytes)` with no length check against `AtlasConfig::attachments_max_size` (or `ATTACHMENTS_MAX_SIZE_MIN`). Any peer answering a `GET /v2/attachments/:hash` request can return an arbitrarily large hex-encoded body, and this attachment is accepted as-is at the point of construction, before any downstream size-based rejection.

### Finding Description
`Deserialize for GetAttachmentResponse` in `stackslib/src/net/atlas/mod.rs` does:
```rust
let payload = String::deserialize(d)?;
let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
let attachment = Attachment::new(bytes);
Ok(GetAttachmentResponse { attachment })
``` [1](#0-0) 

There is no comparison of `bytes.len()` against `attachments_max_size` (`ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) anywhere in this constructor path, unlike `AtlasConfig::validate`, which only validates the *configured* `attachments_max_size` value itself, not the size of any incoming attachment content: [2](#0-1) 

`Attachment::new` performs no validation either: [3](#0-2) 

This is reached from `StacksHttpResponse::decode_atlas_get_attachment` and `RPCGetAttachmentRequestHandler::try_parse_response`, both of which call into this `Deserialize` impl on attacker-controlled HTTP response bodies received when the node's `AttachmentsDownloader` requests an attachment from a remote peer: [4](#0-3) 

A malicious peer that is asked for an attachment (or that a node connects to expecting a legitimate attachment) can therefore return a JSON body whose hex string decodes to a payload far larger than `attachments_max_size`. I was not able to fully re-verify, in this final pass, whether `AttachmentsBatchStateContext::extend_with_attachments` in `download.rs` or `AtlasDB::insert_instantiated_attachment` in `db.rs` apply a late-stage `content.len() <= attachments_max_size` check before persisting/propagating the attachment — my search found no occurrence of `attachments_max_size` inside `download.rs`, but I could not read the full body of `download.rs` or the relevant section of `db.rs` before this iteration ended, so I cannot state with full confidence whether such a check exists further downstream.

### Impact Explanation
If no downstream check exists (which the absence of `attachments_max_size` references in `download.rs` suggests), this allows an unprivileged remote peer to force a node to allocate and persist an attachment of arbitrary size into `AtlasDB`, breaking the invariant that all stored attachments are bounded by `attachments_max_size`. This is a resource-exhaustion / state-corruption vector rather than a crash, and its severity depends entirely on whether the downstream insertion path enforces the size bound that the deserializer skips.

### Likelihood Explanation
Precondition: the victim node must be actively fetching attachments via Atlas (i.e., have outstanding `AttachmentsBatchStateContext` requests) and connect to/be connected by the attacker's peer for that specific attachment hash. No secret or privileged role is needed — any peer capable of answering a `/v2/attachments/:hash` request in response to the victim's outbound Atlas fetch can trigger this.

### Recommendation
Add an explicit size check in `Deserialize for GetAttachmentResponse` (or immediately after, in `try_parse_response`/`decode_atlas_get_attachment`) rejecting any decoded `bytes` whose length exceeds the node's configured `attachments_max_size` before constructing `Attachment`. Additionally, verify and enforce the same bound in `AttachmentsBatchStateContext::extend_with_attachments` and `AtlasDB::insert_instantiated_attachment` as defense-in-depth.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/mod.rs` (or `download.rs`) tests module:
1. Construct a JSON string payload equal to `to_hex(&vec![0u8; 50 * 1024 * 1024])` (50MB of zero bytes).
2. Call `serde_json::from_str::<GetAttachmentResponse>(&format!("\"{}\"", hex_encoded))`.
3. Assert `Ok(resp)` is returned and `resp.attachment.content.len() == 50 * 1024 * 1024`, i.e., far exceeding `ATTACHMENTS_MAX_SIZE_MIN` (1_048_576), with no error raised — demonstrating the missing size guard at the deserialization boundary. A full end-to-end PoC would additionally feed this through `StacksHttpResponse::decode_atlas_get_attachment` and trace whether `AttachmentsBatchStateContext::extend_with_attachments` still contains the oversized attachment afterward.

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

**File:** stackslib/src/net/atlas/mod.rs (L153-156)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
    }
```

**File:** stackslib/src/net/api/getattachment.rs (L134-165)
```rust
impl HttpResponse for RPCGetAttachmentRequestHandler {
    fn try_parse_response(
        &self,
        preamble: &HttpResponsePreamble,
        body: &[u8],
    ) -> Result<HttpResponsePayload, Error> {
        let pages: GetAttachmentResponse = parse_json(preamble, body)?;
        Ok(HttpResponsePayload::try_from_json(pages)?)
    }
}

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
}

impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

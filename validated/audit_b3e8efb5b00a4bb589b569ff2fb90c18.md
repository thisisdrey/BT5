### Title
`attachments_max_size` not enforced when parsing `/v2/attachments/{hash}` responses — unbounded hex-decode before size/hash check - ([File: stackslib/src/net/atlas/mod.rs])

### Summary
`AtlasConfig.attachments_max_size` is validated only for local configuration (`AtlasConfig::validate`) and for attachments accepted via gossiped inventories in `db.rs`, but it is never consulted when a node parses the HTTP response body of a `GET /v2/attachments/{hash}` request it itself issued to a peer. `GetAttachmentResponse::deserialize` unconditionally calls `hex_bytes(&hex_encoded)` on the full response string with no length cap, so a malicious peer serving that RPC endpoint can force the requesting node to allocate and hex-decode an arbitrarily large buffer before any size or hash validation occurs.

### Finding Description
The claimed broken equality is: "bytes allocated during parsing" should be `<= attachments_max_size`, but nothing enforces that bound at the parse site.

- `RPCGetAttachmentRequestHandler::try_parse_response` calls `parse_json::<GetAttachmentResponse>(preamble, body)` directly on the raw response body with no pre-check on `body.len()`. [1](#0-0) 
- `parse_json` simply calls `serde_json::from_slice(body)`, with no cap tied to any attachment-size constant. [2](#0-1) 
- `GetAttachmentResponse::deserialize` reads the JSON string and immediately hex-decodes the entire payload with `hex_bytes(&hex_encoded)`, allocating a `Vec<u8>` proportional to the attacker-controlled hex string length, with no comparison to `AtlasConfig::attachments_max_size` anywhere in this function. [3](#0-2) 
- The alternate decode path `StacksHttpResponse::decode_atlas_get_attachment`, used by the actual download state machine (`AttachmentsBatchStateContext::extend_with_attachments`), goes through the same `GetAttachmentResponse` `Deserialize` impl via `serde_json::from_value`, so it inherits the identical unbounded decode. [4](#0-3) [5](#0-4) 
- A grep across the repo confirms `attachments_max_size` is only referenced in `config/mod.rs`, `atlas/db.rs`, and `atlas/mod.rs`'s `validate()`/tests — never in `getattachment.rs` or `download.rs`, i.e. never on this HTTP response-parsing code path.

Only after this full decode does the caller in the download state machine insert the `Attachment` into `self.attachments` for later hashing/storage — the size limit that exists elsewhere (e.g. checked when attachments are later persisted via `AtlasDB`, per `db.rs`'s use of `attachments_max_size`) runs too late to stop the decode-time allocation and CPU cost of hex-decoding.

### Impact Explanation
A remote peer that a node contacts to download an attachment (a role any unprivileged peer can occupy, since attachment inventories/instances are gossiped and any peer's URL can be chosen as a download source) can respond to `GET /v2/attachments/{hash}` with a JSON string containing hundreds of MB (or more, bounded only by whatever generic HTTP body/content-length limits exist upstream) of hex text. The requesting node must allocate the string, then allocate and populate the decoded byte buffer, before any comparison against `attachments_max_size` or the requested hash occurs. This is a bounded-compute/memory DoS on the attachment-download read path: repeatable per request/per peer interaction, and it burns CPU/memory on the honest fetcher disproportionate to the size the node's own configuration intends to accept.

### Likelihood Explanation
Preconditions: an attacker-controlled node must be running with a StacksHttpResponse capable RPC endpoint and must have been selected by an honest node as a download peer for some attachment (achievable by legitimately gossiping attachment inventories that this attacker peer claims to hold, requiring no privileged role, secret, or elevated access). Cost to the attacker is a single crafted RPC response per request; the node can perform this every time it is chosen as a download source, so it is repeatable. No signature, auth secret, or additional access is required beyond normal, unprivileged P2P/RPC participation.

### Recommendation
Enforce a size cap when parsing attachment responses, before or during hex-decoding, e.g.: check `preamble.get_content_length()` (or `body.len()`) against `2 * attachments_max_size` (to account for hex expansion) in `RPCGetAttachmentRequestHandler::try_parse_response` before calling `parse_json`, and/or pass `attachments_max_size` into `GetAttachmentResponse::deserialize`/a bounded variant so the hex string length is validated prior to allocating the decoded buffer. Additionally validate the final decoded length in `decode_atlas_get_attachment` against `attachments_max_size` before returning `GetAttachmentResponse` to `extend_with_attachments`.

### Proof of Concept
```rust
// stackslib/src/net/api/tests/getattachment.rs (new test)
#[test]
fn test_oversized_attachment_response_dos() {
    use crate::net::http::{HttpContentType, HttpResponsePreamble};
    use crate::net::atlas::GetAttachmentResponse;

    // Simulate a malicious peer's response body: a JSON string containing
    // e.g. 200_000_000 hex chars (100MB decoded), far beyond
    // ATTACHMENTS_MAX_SIZE_MIN (1_048_576 bytes).
    let oversized_hex = "ab".repeat(100_000_000); // 200MB of hex text
    let body = format!("\"{}\"", oversized_hex);

    let preamble = HttpResponsePreamble::new_serialized(
        /* ...construct 200 OK application/json preamble... */
    ).unwrap();

    let handler = getattachment::RPCGetAttachmentRequestHandler::new();

    // Assert: this call proceeds to allocate and hex-decode the full
    // 100MB buffer with no check against AtlasConfig::attachments_max_size
    // before returning Ok, demonstrating the missing bound.
    let result = handler.try_parse_response(&preamble, body.as_bytes());
    assert!(result.is_ok()); // decode succeeds despite far exceeding attachments_max_size
}
```
Instrument with a memory/CPU profiler (or `#[test]` timing assertion) around `GetAttachmentResponse::deserialize`'s `hex_bytes` call in `stackslib/src/net/atlas/mod.rs:73` to show allocation/CPU work scales with attacker-supplied body size with no early rejection.

### Citations

**File:** stackslib/src/net/api/getattachment.rs (L134-143)
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
```

**File:** stackslib/src/net/api/getattachment.rs (L158-166)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
}
```

**File:** stackslib/src/net/http/common.rs (L88-107)
```rust
pub fn parse_json<T: serde::de::DeserializeOwned>(
    preamble: &HttpResponsePreamble,
    body: &[u8],
) -> Result<T, Error> {
    // content-type has to be JSON
    if preamble.content_type != HttpContentType::JSON {
        return Err(Error::DecodeError(
            "Invalid content-type: expected application/json".to_string(),
        ));
    }

    let item_result: Result<T, serde_json::Error> = serde_json::from_slice(body);
    item_result.map_err(|e| {
        if e.is_eof() {
            Error::UnderflowError("Not enough bytes to parse JSON".to_string())
        } else {
            Error::DecodeError(format!("Failed to parse JSON: {:?}", &e))
        }
    })
}
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

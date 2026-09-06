### Title
Reliability score inflation via unverified attachment content in `extend_with_attachments` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` calls `report.bump_successful_requests()` whenever a peer's HTTP response decodes successfully via `decode_atlas_get_attachment()`, without ever checking that the returned `Attachment`'s hash matches the `content_hash` that was actually requested in the corresponding `AttachmentRequest`. A remote peer can therefore inflate its `ReliabilityReport` by returning any well-formed, hex-decodable attachment payload for every request, regardless of correctness.

### Finding Description
The broken equality is: *reported peer reliability == peer actually served the requested content*. This is never enforced.

`extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558) iterates over `results.succeeded`, which pairs each `AttachmentRequest` (which carries the target `content_hash` field, see `stackslib/src/net/atlas/download.rs:467-472`) with the raw HTTP response from that peer:

```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
``` [1](#0-0) 

`decode_atlas_get_attachment()` (stackslib/src/net/api/getattachment.rs:159-165) only parses the JSON body into a `GetAttachmentResponse` — a hex string turned into an `Attachment { content }` via `Deserialize` (stackslib/src/net/atlas/mod.rs:69-77). There is no comparison against `request.content_hash` anywhere in this path. [2](#0-1) [3](#0-2) 

Contrast with the inventory-response case, `extend_with_inventories`, which has the identical unconditional-bump pattern for decode success (stackslib/src/net/atlas/download.rs:507-522) — same class of issue but out of scope per the question, which focuses on the attachment path.

`request.get_url()` identifies which peer's `ReliabilityReport` (keyed by `UrlString` in `self.peers`) gets bumped [4](#0-3) . These per-peer reports are persisted back into `AttachmentsDownloader::reliability_reports` after `Done` state [5](#0-4)  and subsequently reused to build the priority queues for both `AttachmentsInventoryRequest` and `AttachmentRequest` (`get_prioritized_attachments_inventory_requests`, `get_prioritized_attachments_requests`, stackslib/src/net/atlas/download.rs:376-478), where each source's `ReliabilityReport` clone is attached to the request for ordering/selection.

Downstream, mismatched-hash attachments do not get falsely bound to the requested `AttachmentInstance`: in `AttachmentsDownloader::run`, resolved attachments are looked up and stored keyed by `attachment.hash()` (the actual content hash of what was returned), and `find_all_attachment_instances(&attachment.hash())` will simply find no matching instances for an unrelated hash [6](#0-5) . So no wrong-attachment-instance binding occurs — the concrete, exploitable effect is confined to reliability-score inflation, not corrupted attachment resolution.

### Impact Explanation
A malicious/lazy peer that is queried for `AttachmentRequest`s can return arbitrary valid-looking hex-encoded JSON (any bytes, unrelated to the requested `content_hash`) and have `bump_successful_requests()` invoked every time, unconditionally raising its `ReliabilityReport` score. This biases the priority ordering used to select which peers get preferred for future `AttachmentsInventoryRequest`/`AttachmentRequest` traffic (stackslib/src/net/atlas/download.rs:376-478), causing the node to preferentially route future attachment-sync bandwidth/requests toward the misbehaving peer over more honest ones. It does not, however, cause a wrong attachment to be bound to a instance/BNS name mismatch, since resolution is keyed by the returned content's actual hash — so no forged/incorrect attachment data is served as canonical to instance consumers. The effect is a resource/priority-steering degradation of the Atlas attachment-sync subsystem, not corruption of consensus-relevant chain state.

### Likelihood Explanation
Precondition is trivial: the attacker only needs to be an outbound sync peer that the node queries at least once via `AttachmentRequest` for some `content_hash`, which happens naturally for any peer with an advertised data URL that appears in the peer's inventory as having some attachment (stackslib/src/net/atlas/download.rs:404-478). No secrets, no privileged role, and no RPC auth needed — this is standard Atlas peer-to-peer download traffic. The attack is fully repeatable per request/response cycle.

### Recommendation
In `extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before calling `report.bump_successful_requests()`; treat a hash mismatch as a failed/faulty response (`bump_failed_requests()`, and optionally treat it as more severe than a decode failure, e.g. flagging the peer as faulty) rather than a success.

### Proof of Concept
Rust test plan (net/atlas module test):
1. Construct an `AttachmentRequest` with a known `content_hash` H1 (e.g. hash of `b"real content"`).
2. Build a mock `StacksHttpResponse` whose body is a valid `GetAttachmentResponse`-shaped JSON encoding hex of unrelated bytes `b"unrelated"` (hash H2 != H1).
3. Populate `BatchedRequestsResult.succeeded` with `(request, Some(mock_response))` for several iterations, all returning content that never matches H1.
4. Call `AttachmentsBatchStateContext::extend_with_attachments` on a context whose `peers` map contains the peer's `ReliabilityReport::empty()`.
5. Assert: `report.successful_requests_count` (or equivalent field exercised by `bump_successful_requests`) increased on every call, despite `response.attachment.hash() != request.content_hash` for all responses — demonstrating the reliability score inflates independent of correctness.

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

**File:** stackslib/src/net/atlas/download.rs (L182-185)
```rust
                // Update reliability reports
                for (peer_url, report) in context.peers.drain() {
                    self.reliability_reports.insert(peer_url, report);
                }
```

**File:** stackslib/src/net/atlas/download.rs (L534-538)
```rust
        for (request, response) in results.succeeded.drain() {
            let report = self
                .peers
                .get_mut(request.get_url())
                .expect("Atlas: unable to retrieve reliability report for peer");
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

Confirmed: `extend_with_attachments` at [1](#0-0)  calls `response.decode_atlas_get_attachment()` and, on `Ok`, unconditionally inserts `response.attachment` into `self.attachments` — with no comparison against `request.content_hash` (the `AttachmentRequest.content_hash` that was used to select this specific request). The request's `content_hash` field is dropped entirely once the response is fetched.

### Title
Attachment content is accepted without verifying `Hash160::from_data(content) == requested content_hash` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`GetAttachmentResponse::deserialize` builds an `Attachment` straight from the hex string in the HTTP response body with no relation to the requested hash, and its only consumer, `AttachmentsBatchStateContext::extend_with_attachments`, stores whatever bytes came back without checking them against the `content_hash` that was requested via `AttachmentRequest`. A malicious or compromised peer serving `GET /v2/attachments/{hash}` can return arbitrary bytes for any requested hash and have them accepted as the canonical attachment content.

### Finding Description
The client requests an attachment by hash via `StacksHttpRequest::new_getattachment` (`stackslib/src/net/api/getattachment.rs` lines 145-156), formatting the path as `/v2/attachments/{attachment_id}`. On the response side, `StacksHttpResponse::decode_atlas_get_attachment` (lines 158-165) just JSON-decodes the body into `GetAttachmentResponse`, whose `Deserialize` impl (`stackslib/src/net/atlas/mod.rs` lines 69-77) does:
```
let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
let attachment = Attachment::new(bytes);
```
with no reference at all to the hash that was in the request path. The broken equality is: `Hash160::from_data(&decoded_attachment.content) == requested_content_hash` is never checked anywhere between decode and storage.

The only downstream consumer, `AttachmentsBatchStateContext::extend_with_attachments` (`download.rs` lines 530-558), iterates `results.succeeded` (keyed by `AttachmentRequest`, which does carry `content_hash`), calls `response.decode_atlas_get_attachment()`, and on success does `self.attachments.insert(response.attachment)` — it never compares `response.attachment.hash()` (i.e., `Hash160::from_data`) to `request.content_hash`. Back in `AttachmentsDownloader::run` (lines 152-170), each attachment from the resulting set is inserted via `network.atlasdb.insert_instantiated_attachment(&attachment)` and paired with `find_all_attachment_instances(&attachment.hash())` — using the *actual* hash of the forged data, not the originally requested one. This means a malicious peer can supply any bytes for an attachment whose hash appears in a legitimate on-chain attachment instance (e.g., a BNS zonefile commit), and the node will accept, store, and later serve/gossip that forged content as if it were the data actually committed to on-chain via the BNS `content_hash`.

### Impact Explanation
A single unprivileged remote peer that a node syncs Atlas attachments from can return attacker-controlled bytes in response to any `/v2/attachments/{hash}` request. Because there's no hash-equality check, the forged `Attachment` is stored in `AtlasDB` via `insert_instantiated_attachment` and gets bound to the real (unrelated) `AttachmentInstance`(s) that reference that requested hash in `find_all_attachment_instances(&attachment.hash())`. Since `attachment.hash()` is computed from the forged bytes, not from the requested hash, it could bind to a *different* legitimate attachment instance than intended, or simply corrupt the record the node believes corresponds to the on-chain BNS commitment. This is a High-severity "attachment/BNS mismatch" — serving/state-storing content that does not match what was actually committed on-chain, and it can be repeated for every attachment hash the node tries to sync.

### Likelihood Explanation
Any full/relay node running Atlas (BNS zonefile) sync will, in the ordinary course of operation, request unresolved attachments from its outbound peer set (`network.get_outbound_sync_peers()`); no special peer privilege, secret, or configuration is required beyond being one of the node's data-URL peers. The attacker just needs to run a normal Stacks node/peer that the victim syncs attachments from and respond to a `GET /v2/attachments/{hash}` request with a well-formed JSON hex-string body containing arbitrary content — no signature or cryptographic proof is checked at any layer of this path.

### Recommendation
In `GetAttachmentResponse::deserialize` or (preferably) at the call site of `decode_atlas_get_attachment` in `extend_with_attachments`, verify `Hash160::from_data(&attachment.content) == request.content_hash` before accepting/storing the attachment; drop and treat the peer as faulty (`report.bump_failed_requests()`) otherwise, similar to how `content_hash == Hash160::empty()` is already special-cased elsewhere in `download.rs`.

### Proof of Concept
Rust test plan (in `stackslib/src/net/api/tests/getattachment.rs` or a new atlas download test):
1. Construct a `StacksHttpResponse` with an `HttpResponsePreamble` (`ok_json`) and a JSON body `"<hex of arbitrary bytes unrelated to any real attachment>"`.
2. Call `response.decode_atlas_get_attachment()`.
3. Assert it returns `Ok(GetAttachmentResponse { attachment })` where `attachment.hash() != requested_hash` (the hash used to build the original `AttachmentRequest`/URL path) — proving no equality check exists.
4. Optionally extend to `AttachmentsBatchStateContext::extend_with_attachments` with a `BatchedRequestsResult` whose `succeeded` map has an `AttachmentRequest{ content_hash: H1, .. }` paired with a response containing bytes hashing to `H2 != H1`, and assert `context.attachments` contains the forged attachment despite the mismatch.

### Citations

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

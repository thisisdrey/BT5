### Title
Forged attachment content bypasses hash verification in `extend_with_attachments` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GetAttachmentResponse` and unconditionally inserts `response.attachment` into `self.attachments` without ever checking that `Attachment::hash(&response.attachment.content) == request.content_hash`. Any remote peer selected to serve a `GET /v2/attachments/{content_hash}` request can therefore return arbitrary attachment bytes that get accepted into the local Atlas attachment set as if they matched the committed hash.

### Finding Description
The invariant that should hold for every accepted attachment download is `Attachment::hash(response.attachment.content) == request.content_hash` (the hash requested via `AttachmentRequest::make_request_type`, built from the on-chain committed `content_hash` in an `AttachmentInstance`). The code at `stackslib/src/net/atlas/download.rs:530-559` shows this equality is never checked: [1](#0-0) 

For each `(request, response)` pair in `results.succeeded`, the function only checks that `response.decode_atlas_get_attachment()` succeeds (i.e., the HTTP body parses into a well-formed `GetAttachmentResponse`), then does `self.attachments.insert(response.attachment)` — with no comparison of `response.attachment`'s content hash against `request.content_hash`. This is in contrast to the analogous inventory-handling function `extend_with_inventories` a few lines above, which at least validates decodability but similarly performs no cross-check against expected values beyond decode success — however for attachments, decode success alone says nothing about content correctness, since the hash is the entire point of the request.

A malicious peer that is queried for a given `content_hash` (reachable simply by being selected as an outbound sync peer serving Atlas attachments — no privileged role, secret, or key required) can respond to `GET /v2/attachments/{content_hash}` with HTTP 200 and any well-formed `GetAttachmentResponse` body whose `attachment.content` hashes to a different value than the requested `content_hash`. This decodes successfully and is inserted into `self.attachments` unconditionally.

### Impact Explanation
The `self.attachments` set feeds into `AttachmentsDownloader::run`'s `resolved_attachments: Vec<(AttachmentInstance, Attachment)>`, which is the data BNS name resolution / Atlas attachment storage consumes as "resolved" content for a given on-chain committed `content_hash`. Because no hash check gates insertion, a remote unprivileged peer can cause the node to accept and later serve/store attachment content that was never actually committed on-chain for that hash — i.e., non-canonical data served as canonical, matching the "attachment/BNS mismatch" High-severity category. This is repeatable for every attachment request the malicious peer is asked to serve.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to run a normal peer with an advertised data URL that the victim node selects as an outbound sync peer for attachment downloads (a normal, permissionless peer-selection outcome), and then respond to attachment GET requests with malformed-content-but-well-formed-response bytes. No RPC secret, no StackerDB slot ownership, no signature forgery is needed — just returning an HTTP 200 body that deserializes into `GetAttachmentResponse`. This is trivially and repeatably exploitable by any remote peer serving Atlas data.

### Recommendation
In `extend_with_attachments`, after `response.decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` (mirroring the check that exists in the upstream/canonical implementation of this function) before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) rather than inserting the attachment.

### Proof of Concept
Rust test in `stackslib::net::atlas::download` (or a new test module alongside `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AttachmentInstance`/`AttachmentRequest` with `content_hash = H` (e.g., `Hash160::from_data(b"expected")`).
2. Construct a `GetAttachmentResponse` whose `attachment = Attachment::new(b"forged content".to_vec())`, where `Attachment::hash(b"forged content") != H`.
3. Build a `BatchedRequestsResult<AttachmentRequest>` with `succeeded = {request.clone() -> Some(response_wrapping_the_forged_GetAttachmentResponse)}`, populate `self.peers` with a `ReliabilityReport` for `request.get_url()`.
4. Call `context.extend_with_attachments(&mut results)`.
5. Assert `context.attachments.contains(&forged_attachment) == true` and that no equivalent check rejected it — proving `Attachment::hash(forged_attachment.content) != H` was inserted despite the mismatch, at `stackslib/src/net/atlas/download.rs:547-549`.

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

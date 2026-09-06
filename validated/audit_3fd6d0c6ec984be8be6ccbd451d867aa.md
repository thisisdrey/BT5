Confirmed: `decode_atlas_get_attachment` in `stackslib/src/net/api/getattachment.rs` (lines 158-165) simply JSON-decodes the response body into a `GetAttachmentResponse` with no hash verification against the `AttachmentRequest.content_hash` that was originally sent. `extend_with_attachments` in `stackslib/src/net/atlas/download.rs` (lines 530-559) inserts `response.attachment` into `self.attachments: HashSet<Attachment>` without ever comparing `Hash160::from_data(&content)` to the request's `content_hash`. The consumer, `AttachmentsDownloader::run` (lines 152-169), then computes `attachment.hash()` from the (possibly wrong) content bytes and calls `atlasdb.insert_instantiated_attachment(&attachment)`, and separately calls `context.attachments_batch.resolve_attachment(&attachment.hash())` — again keyed on the wrongly-derived hash, not on the originally requested `H`.

### Title
Missing content-hash verification on Atlas attachment download responses lets a malicious peer poison/desync attachment resolution - (File: stackslib/src/net/atlas/download.rs)

### Summary
When the Atlas downloader fetches an attachment for a requested `content_hash = H` via `AttachmentRequest`, the response is decoded and stored without verifying that `Hash160::from_data(&content) == H`. A malicious peer that is queried for attachment `H` (because it advertised having it in its `AttachmentsInv`) can instead return arbitrary bytes, causing the node to store and "resolve" the batch entry for `H` using the wrong hash, while `H` itself is never satisfied.

### Finding Description
In `stackslib/src/net/atlas/download.rs`, `AttachmentsBatchStateContext::extend_with_attachments` (lines 530-559) iterates `results.succeeded`, which maps each `AttachmentRequest{content_hash: H, ...}` to an `Option<StacksHttpResponse>` coming directly from an outbound HTTP GET to a peer URL from `self.peers` (`AttachmentRequest.sources`) — any peer that claimed (via `GetAttachmentsInvResponse`) to have `H`, which is unauthenticated peer-supplied data.

The code calls `response.decode_atlas_get_attachment()` (`stackslib/src/net/api/getattachment.rs` lines 158-165), which only performs `get_http_payload_ok()` and `serde_json::from_value` — no equality check against the original `H` that was requested (the `request` value, which holds `content_hash`, is discarded after being used to look up `report`). The decoded `Attachment{content}` is inserted directly into `self.attachments: HashSet<Attachment>` (line 548) keyed by whatever `Attachment`'s own `Hash`/`Eq` impl uses (based on `content` bytes, per `stackslib/src/net/atlas/mod.rs`), not by `H`.

Later, `AttachmentsDownloader::run` (lines 152-169) drains `context.attachments`, computes `attachment.hash()` (i.e., `Hash160::from_data(&content)` on the *actual* bytes received) and calls `network.atlasdb.insert_instantiated_attachment(&attachment)` — storing the attachment under its true (wrong) hash rather than `H`. It also calls `context.attachments_batch.resolve_attachment(&attachment.hash())`, marking the batch entry keyed by the wrong hash as resolved. The batch entry actually corresponding to `H` (from `AttachmentInstance.content_hash`) is never resolved by this response, since `resolve_attachment` is keyed on `attachment.hash()`, not on the original request's `content_hash`.

The root cause: no equality assertion `Hash160::from_data(&attachment.content) == request.content_hash` exists anywhere between decode and insertion/resolution in this path.

### Impact Explanation
A remote, unprivileged peer that is included in `self.peers` (any outbound sync peer reachable over the node's RPC/data URL, requiring no secret or privileged role) can respond to a legitimate `AttachmentRequest` for `H` with a 200 body containing different bytes. This causes:
- The true content for `H` to never be stored via this response (subsequent `/v2/attachments/{H}` lookups via `getattachment.rs` lines 104-118 will 404 unless resolved through another, honest path/peer).
- Garbage/attacker-chosen content to be inserted into `atlasdb` under an attacker-controlled hash value (not `H`), polluting the attachment store.
- The `AttachmentsBatch` entry is not correctly resolved for `H` (since resolution is keyed by the wrong hash), potentially causing repeated retries for `H` until `max_attachment_retry_count` is exhausted, after which the batch is dropped and `H` is permanently unresolved for that batch — a BNS attachment/state mismatch (data the node claims doesn't correspond to what the chain committed via `AttachmentInstance.content_hash`).

This matches the "High - attachment/BNS mismatch" category: content served (or attempted to be resolved) does not match what was actually requested/committed on-chain, and the true content becomes unreachable through this batch's resolution path.

### Likelihood Explanation
The attacker only needs to be one of the `network.get_outbound_sync_peers()` with a reachable data URL, and to have previously advertised (or be believed to have) the attachment `H` in its inventory response — both are attacker-controlled/no-privilege actions (an unprivileged peer can freely gossip fake `GetAttachmentsInvResponse` data). No secret, signature, or privileged role is required; a single crafted HTTP 200 response per targeted `content_hash` triggers the mismatch, and this is repeatable for every attachment the node tries to fetch from that peer.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after calling `response.decode_atlas_get_attachment()`, explicitly verify `Hash160::from_data(&response.attachment.content) == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()` and optionally penalize/blacklist the peer), and do not insert or resolve the batch entry with the wrong hash.

### Proof of Concept
1. In `stackslib/src/net/atlas/download.rs` tests, construct an `AttachmentsBatchStateContext` with a single peer and reliability report.
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has one entry: key = `AttachmentRequest{content_hash: H, sources, ...}`, value = `Some(StacksHttpResponse)` built from a JSON body `GetAttachmentResponse{attachment: Attachment{content: b"attacker-bytes".to_vec()}}` where `Hash160::from_data(b"attacker-bytes") != H`.
3. Call `context.extend_with_attachments(&mut results)`.
4. Assert: `context.attachments` contains an `Attachment` whose `Hash160::from_data(&content) != H`, and that no entry in `context.attachments` has hash equal to `H` — demonstrating the request for `H` was silently satisfied with mismatched content and would be mis-resolved by `AttachmentsDownloader::run`.
### Title
Missing content-hash verification of downloaded `Attachment` content lets a malicious peer poison reliability scoring and permanently block resolution of a validly committed attachment - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `GetAttachmentResponse` that parses as valid JSON and inserts its `attachment` into `self.attachments` without ever checking that `attachment.hash()` equals the `content_hash` of the `AttachmentRequest` that was sent. Because the reliability report is bumped as "successful" purely on the basis of a decodable HTTP response (not on hash correctness), a malicious peer that always answers with garbage content is treated as reliable forever, is repeatedly selected via `get_most_reliable_source`, and the real attachment instance is never resolved until the batch's `max_attachment_retry_count` is exhausted and the batch is dropped.

### Finding Description
The broken equality is: `attachment.hash() == content_hash` (the hash requested in the `AttachmentRequest`) is never checked at the point the response is consumed.

- `AttachmentsBatchStateContext::get_prioritized_attachments_requests` (stackslib/src/net/atlas/download.rs:404-478) builds an `AttachmentRequest` for a `content_hash` whenever any peer's inventory bit at the correct page/position is `1` — this bit is entirely peer-controlled from the `GetAttachmentsInvResponse` decoded in `extend_with_inventories` (download.rs:490-528), with no cross-check against actual possession.
- Once a `GetAttachmentResponse` for that request arrives, `extend_with_attachments` (download.rs:530-558) does:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
```
There is no comparison of `response.attachment.hash()` to `request.content_hash`. Any well-formed JSON body counts as a "successful request" for reliability purposes.
- In `AttachmentsDownloader::run`, the `Done` branch (download.rs:152-169) then looks up matching `AttachmentInstance`s by `attachment.hash()` (the *actual* hash of the bogus bytes) via `find_all_attachment_instances(&attachment.hash())`, and calls `context.attachments_batch.resolve_attachment(&attachment.hash())`. Since the malicious content's real hash never equals the originally requested `content_hash`, the entry for that `content_hash` is never removed from `attachments_batch.attachments_instances`.
- Because the malicious peer's `ReliabilityReport` was bumped as successful (garbage content still parses), that peer's reported reliability is at least as good as (often better than) honest peers, so `AttachmentRequest::get_most_reliable_source`/`Ord` implementation (download.rs:1088-1096) continues to prefer it on every subsequent retry cycle, since `self.reliability_reports` persists across batch retries in `AttachmentsDownloader` (download.rs:47, 118-123).
- Each retry bumps `AttachmentsBatch::bump_retry_count` (download.rs:1183-1194, capped by `MAX_RETRY_DELAY`), and after `retry_count >= max_attachment_retry_count` the batch is dropped entirely (download.rs:187-205), permanently abandoning the attachment instance even though honest peers may have served correct content in the same or other rounds (their responses are indistinguishable from the attacker's because nothing validates hash correctness).

No existing guard (handshake, chunk signature, `MAX_MESSAGE_LEN`, etc.) mitigates this, because the fault is a missing application-level equality check between the wire-supplied attachment bytes and the hash committed on-chain.

### Impact Explanation
A single malicious peer (no privileged role required — any peer that can be selected as an "outbound sync peer" and answer HTTP `/v2/attachments/inv` and `/v2/attachments/:hash` RPC requests) can:
1. Always advertise `has_attachment=1` for a content hash it does not possess or has different bytes for.
2. Always answer `GET /v2/attachments/:hash` with arbitrary bytes.
3. Get treated as maximally reliable indefinitely (never marked failed), so its reports/sources keep winning `get_most_reliable_source` selection across retries.
4. Force the batch containing that attachment instance to exhaust `max_attachment_retry_count` and be dropped, permanently leaving the attachment instance unresolved in the node's Atlas DB even though the on-chain name operation committed a real, resolvable hash.

This matches the "High: serving non-canonical state as canonical / attachment-BNS mismatch" category — BNS name resolution on this node will report "not found" for an attachment hash that a confirmed transaction actually committed, which is a state-integrity impact distinct from mere transient unavailability, and it is fully repeatable by the same attacker for every attachment instance it can see in the inventory protocol.

### Likelihood Explanation
- Precondition: the victim node must have the attacker as one of its `outbound_sync_peers` (i.e., a normal, unprivileged P2P peer connection) with a reachable data URL — this is a standard, unauthenticated interaction the Atlas subsystem already performs with arbitrary peers.
- Attacker cost is trivial: run a peer, answer the two standard RPC endpoints (`getattachmentsinv`, `getattachment`) with fabricated JSON.
- No secret, no signature, no StackerDB ownership, no admin role required.
- Effect is deterministic and repeatable for every attachment content hash the attacker chooses to claim.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments` and before crediting the peer's `ReliabilityReport` with a success; on mismatch, treat it as a failed request (`bump_failed_requests`) and optionally record/deregister the offending peer (similar to `faulty_peers` handling for inventories) so it stops being selected as a reliable source for that or other attachments.

### Proof of Concept
Add a Rust unit test in `stackslib/src/net/atlas/tests.rs` (or a new test in `download.rs`'s existing `#[cfg(test)]` module) that:
1. Constructs an `AttachmentsBatchStateContext` with a single `AttachmentsBatch` tracking one `AttachmentInstance` with a known `content_hash`.
2. Feeds a fabricated `GetAttachmentsInvResponse` (via `extend_with_inventories`) with `inventory[position] = 1` for a mock peer URL, though the peer has no matching data.
3. Feeds a `GetAttachmentResponse` with `attachment.content` whose `Hash160::from_data` does **not** equal `content_hash` (via `extend_with_attachments`).
4. Assert that after `extend_with_attachments`, the peer's `ReliabilityReport::bump_successful_requests` was invoked (showing the flaw: transport success ≠ content correctness).
5. Assert that `attachments_batch.attachments_instances` still contains the original `attachment_index`/`content_hash` entry (i.e., `resolve_attachment` never removed it), proving the request will be re-queued.
6. Loop calling `bump_retry_count()` until `retry_count >= max_attachment_retry_count`, confirming the batch is dropped (per the `run()` logic at download.rs:187-205) while the instance was never actually resolved — demonstrating permanent, unauthenticated denial of resolution for a validly committed attachment.
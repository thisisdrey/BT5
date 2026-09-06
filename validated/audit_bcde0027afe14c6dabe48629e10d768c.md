### Title
Missing hash verification on downloaded attachment content lets a peer poison its own reliability score and get perpetually re-selected, starving correct attachment content - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` calls `report.bump_successful_requests()` whenever the HTTP response for `/v2/attachments/<hash>` decodes into a `GetAttachmentResponse`, without checking that `Hash160::from_data(&attachment.content) == request.content_hash`. Because `AttachmentRequest::get_url()`/`get_most_reliable_source()` always routes the (single) real content request to the highest-`ReliabilityReport::score()` peer, an attacker peer can repeatedly serve hash-mismatched attachment bytes, keep inflating its own score, and remain the sole peer queried on every `bump_retry_count()`-delayed retry, while peers holding the correct content are never asked.

### Finding Description
`get_prioritized_attachments_requests` (stackslib/src/net/atlas/download.rs:404-478) builds exactly one `AttachmentRequest` per missing `content_hash`, collecting every peer whose advertised inventory claims to have it into `sources: HashMap<UrlString, ReliabilityReport>`. `Requestable::get_url()` for `AttachmentRequest` (stackslib/src/net/atlas/download.rs:1104-1108) delegates to `get_most_reliable_source()` (stackslib/src/net/atlas/download.rs:1073-1080), which picks the single peer with the maximum `ReliabilityReport::score()` (stackslib/src/net/atlas/download.rs:1299-1305). Only that one peer is actually sent the `GET /v2/attachments/{content_hash}` request for this round.

When the response comes back, `extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558) does:
```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
```
There is no comparison of `response.attachment.hash()` against `request.content_hash`. Any syntactically well-formed hex payload (`GetAttachmentResponse::deserialize`, stackslib/src/net/atlas/mod.rs:69-77) counts as a "successful" request and bumps the peer's `total_requests_sent`/`total_requests_success`, which strictly increases `score()` (stackslib/src/net/atlas/download.rs:1299-1305) and therefore `Ord` ranking (stackslib/src/net/atlas/download.rs:1308-1315).

Downstream, the hash mismatch is caught only in `AttachmentsDownloader::run` (stackslib/src/net/atlas/download.rs:152-169), which resolves batch entries using `attachment.hash()` (the actual received content's hash) rather than the requested `content_hash`. A mismatched attachment therefore fails to resolve the pending instance, the batch is re-enqueued via `bump_retry_count()` (stackslib/src/net/atlas/download.rs:1183-1194, using `MAX_RETRY_DELAY`), and on the next round `get_most_reliable_source()` is queried again from the same (now-persisted, in `AttachmentsDownloader.reliability_reports`) score table — where the attacker peer's score has only gone up. The attacker can therefore stay top-ranked indefinitely by simply answering every request (inv or content) with any parseable payload, regardless of correctness, and single-handedly monopolize the one content request slot per `content_hash` per retry cycle, up to `max_attachment_retry_count`, after which the batch is dropped entirely (stackslib/src/net/atlas/download.rs:187-205).

The claimed equality in the question — "peer selected for retry == peer whose past response hash matched request" — is indeed never enforced: selection is driven purely by `ReliabilityReport::score()`, which is blind to content-hash correctness.

### Impact Explanation
No forged content is ever accepted as canonical (the `attachment.hash()`-keyed resolve logic prevents that), so this is not a Critical data-integrity break. However, an unprivileged outbound peer can durably deny resolution of specific attachments (e.g., BNS name-registration attachments) whose correct content is held only by other, non-adversarial peers, by continuously winning the single-source selection via score inflation. This matches the High-impact bucket "attachment/BNS mismatch" in effect (attachment permanently unresolved despite being available from honest peers), is fully repeatable across retry cycles, and requires no privileged role — only that the node has this peer among `network.get_outbound_sync_peers()` and that peer's `/v2/attachments/inv` claims possession of the target attachment.

### Likelihood Explanation
Preconditions are modest: attacker must be an outbound sync peer of the victim node (routine for any node participating in normal peer gossip/handshake) and must respond to `/v2/attachments/inv` claiming it holds the relevant page/index (a claim that is itself unverified). From there, the attacker's cost is trivial — return any hex-decodable byte string to `/v2/attachments/{hash}` — and the effect compounds every retry (each accepted-but-wrong response further inflates `total_requests_success`/`total_requests_sent`, keeping the attacker's `score()` competitive or better than honest peers who may occasionally fail due to timeouts).

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558), verify `response.attachment.hash() == request.content_hash` before calling `report.bump_successful_requests()`; treat a hash mismatch as a failed request (`report.bump_failed_requests()`) and optionally penalize/blacklist the peer more aggressively. Additionally, consider having `BatchedRequestsState`/`AttachmentRequest` retry against the next-best source within the same round instead of only the single top-ranked peer, so a single poisoned score cannot fully starve retrieval.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or a new unit test module) that:
1. Constructs an `AttachmentsBatchStateContext` with two peers in `self.peers`: `honest_url` with `ReliabilityReport::new(5, 5)` and `attacker_url` seeded with an artificially high `ReliabilityReport::new(50, 50)` (simulating prior "successful" but hash-mismatched rounds).
2. Builds an `AttachmentRequest` whose `sources` includes both peers for a target `content_hash` H, and asserts `get_most_reliable_source()` returns `attacker_url` (confirms selection is score-only).
3. Simulates a `BatchedRequestsResult` where the `succeeded` response for this request decodes to a `GetAttachmentResponse` whose `attachment.hash() != H` (garbage content), and calls `extend_with_attachments`.
4. Asserts (this should currently FAIL, demonstrating the bug) that `context.peers.get(&attacker_url).unwrap().total_requests_success` was NOT incremented for the mismatched response — i.e., add:
```rust
assert_eq!(
    context.peers.get(&attacker_url).unwrap().total_requests_success,
    50, // unchanged, since content hash didn't match
);
```
Currently this assertion fails because `total_requests_success` becomes `51` despite the hash mismatch, confirming the missing verification at stackslib/src/net/atlas/download.rs:547-552.
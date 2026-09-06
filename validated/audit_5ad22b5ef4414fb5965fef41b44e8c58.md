### Title
`extend_with_attachments` bumps `ReliabilityReport` success on any decodable payload without validating `content_hash`, letting a peer game the score used by `AttachmentRequest::get_most_reliable_source`/`Ord` - (stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` marks a peer's `ReliabilityReport` as successful whenever the HTTP response decodes into an `Attachment` struct, without checking that the returned bytes actually hash to the requested `content_hash`. Because `AttachmentRequest`'s `Ord`/`get_most_reliable_source` (and `Requestable::get_url`) select the peer with the highest `ReliabilityReport::score()`, a peer that always answers with a decodable-but-wrong-hash attachment will accumulate an ever-increasing score and be preferentially re-selected for the same `content_hash`, indefinitely starving honest peers' attachment delivery.

### Finding Description
In `stackslib/src/net/atlas/download.rs`, `AttachmentsBatchStateContext::extend_with_attachments`: [1](#0-0) 
only distinguishes between "no response" (`report.bump_failed_requests()`) and "response failed to decode" (`report.bump_failed_requests()`) versus "response decoded" (`report.bump_successful_requests()`). There is no comparison between the decoded `Attachment`'s hash and `request.content_hash` at this point — the equality that should gate scoring (`attachment.hash() == request.content_hash`) simply does not exist here.

The score computed from these counters is used directly to rank peers: [2](#0-1) 
and `AttachmentRequest::get_most_reliable_source`/`Ord`/`Requestable::get_url` pick the peer with the highest score to be queried (and re-queried) for a given `content_hash`: [3](#0-2) 

A malicious peer that is a legitimate outbound sync peer (`network.get_outbound_sync_peers()`) can: (1) answer `/v2/attachments/inv` requests honestly/successfully so it is always included as a "source" for the attacked `content_hash` in `get_prioritized_attachments_requests`, and (2) answer `/v2/attachments/<hash>` GET requests with any well-formed (decodable) attachment payload whose actual content hash does not match `content_hash`. Every such response causes `bump_successful_requests()` (never `bump_failed_requests()`), so `total_requests_success` and hence `score()` keep climbing just like an honest, always-correct peer. The correct hash-mismatch content will not be inserted into `network.atlasdb` as a validated attachment for that `content_hash` (mismatches are rejected further downstream), so the batch is never resolved and gets bumped for retry via `bump_retry_count()`, but the malicious peer's inflated `ReliabilityReport` keeps making it `get_most_reliable_source` on every subsequent retry (`self.reliability_reports` persists across `run()` calls since it is only updated, never decayed, on hash mismatch). This exhausts `max_attachment_retry_count` while the actual content is never fetched, because honest peers rank lower solely due to this scoring gap.

### Impact Explanation
The attacker cannot forge accepted attachment content (a separate hash-check gap at storage time is out of scope here), but it can indefinitely dominate the `reliability_reports` ranking for a `content_hash` it wants to censor/stall, causing the downloader to keep re-querying it instead of any honest source. This matches the in-scope "attachment/BNS mismatch" / steering category: the node repeatedly wastes its retry budget on attacker data and may drop the batch (`"Atlas: dropping batch {:?} retries count exceeded"`) — the attachment for a legitimately committed on-chain reference is never resolved even though honest peers hold the correct data, i.e., attacker steers/starves attachment resolution for arbitrary `content_hash` values indefinitely.

### Likelihood Explanation
Preconditions are minimal and match the unprivileged attacker model: the attacker only needs to run its own peer, be selected as an outbound sync peer by the victim (a normal, non-privileged occurrence for any peer providing a data URL), and respond to normal Atlas inventory/attachment HTTP requests. No secrets, signatures, or privileged roles are required, and the behavior is fully repeatable on every batch/retry cycle at negligible attacker cost (one crafted HTTP response per attachment request).

### Recommendation
In `extend_with_attachments` (download.rs, `AttachmentsBatchStateContext::extend_with_attachments`), after `decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before calling `report.bump_successful_requests()`; otherwise call `report.bump_failed_requests()` (and optionally record the peer as faulty/evict its URL for repeated offenses) so wrong-hash deliveries degrade the offending peer's reliability score just like decode failures do.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or a new net-level test) that:
1. Constructs an `AttachmentsBatchStateContext` with a single peer URL and an `AttachmentRequest` for a known `content_hash`.
2. Simulates `BatchedRequestsResult::succeeded` containing a `StacksHttpResponse` whose decoded `Attachment` content hashes to a *different* value than `content_hash` (i.e., `decode_atlas_get_attachment()` succeeds but `attachment.hash() != content_hash`).
3. Calls `context.extend_with_attachments(&mut results)` repeatedly (e.g., 10 times) and asserts on `context.peers.get(&peer_url).unwrap().score()`.
4. Current behavior: `score()` increases every iteration (since `bump_successful_requests()` is unconditionally invoked on successful decode), proving the malicious peer's `ReliabilityReport` never degrades despite consistently serving wrong-hash content — demonstrating it will always win `AttachmentRequest::get_most_reliable_source`/`Ord` against any peer that ever fails a single request.

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

**File:** stackslib/src/net/atlas/download.rs (L1073-1119)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
    }
}

impl Hash for AttachmentRequest {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.content_hash.hash(state)
    }
}

impl Ord for AttachmentRequest {
    fn cmp(&self, other: &AttachmentRequest) -> Ordering {
        other.sources.len().cmp(&self.sources.len()).then_with(|| {
            let (_, report) = self.get_most_reliable_source();
            let (_, other_report) = other.get_most_reliable_source();
            report.cmp(other_report)
        })
    }
}

impl PartialOrd for AttachmentRequest {
    fn partial_cmp(&self, other: &AttachmentRequest) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Requestable for AttachmentRequest {
    fn get_url(&self) -> &UrlString {
        let (url, _) = self.get_most_reliable_source();
        url
    }

    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".to_string(),
            format!("/v2/attachments/{}", &self.content_hash),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
}
```

**File:** stackslib/src/net/atlas/download.rs (L1299-1306)
```rust
    pub fn score(&self) -> u32 {
        let n = self.total_requests_sent;
        if n == 0 {
            return n;
        }
        self.total_requests_success * 1000 / (n * 1000) + n
    }
}
```

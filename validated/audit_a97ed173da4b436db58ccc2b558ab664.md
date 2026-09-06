### Title
`ReliabilityReport` conflates "peer answered with a well-formed HTTP payload" with "peer delivered hash-correct content", letting a lying peer permanently win attachment-source selection - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` bumps a peer's reliability counters purely based on whether the HTTP response could be decoded into an `Attachment` struct (`decode_atlas_get_attachment()`), never checking that the returned bytes actually hash to the requested `content_hash`. Because `AttachmentRequest::get_most_reliable_source`/`Requestable::get_url` always route the *next* fetch to `max_by_key(|(_, v)| v.score())`, a remote peer that always answers with syntactically valid but wrong-content attachments will keep winning selection over a genuinely correct, lower-scored peer.

### Finding Description
`ReliabilityReport::score()` is derived only from `total_requests_sent`/`total_requests_success` counters [1](#0-0)  and these counters are updated in `extend_with_attachments`: [2](#0-1) 

Here, `bump_successful_requests()` fires as soon as `response.decode_atlas_get_attachment()` succeeds — i.e., the peer sent a well-formed attachment payload over HTTP. There is no comparison of the decoded `Attachment`'s actual content hash against `request.content_hash`, the value that was used to build the outbound request in `AttachmentRequest::make_request_type` (`/v2/attachments/{content_hash}`) [3](#0-2) . The decoded attachment is simply inserted into `self.attachments: HashSet<Attachment>` regardless of correctness.

Selection of which peer to query next is driven entirely by this same, uncorrected score: [4](#0-3) 

and `AttachmentRequest`'s own `Ord` impl (used for the priority queue) also derives from `get_most_reliable_source`, so a peer with an inflated score dominates both the URL choice for the HTTP GET and the batch's retry priority [5](#0-4) .

A malicious peer only needs to keep answering `/v2/attachments/{content_hash}` requests with *any* well-formed `Attachment` HTTP body (wrong bytes for that hash) to keep `total_requests_success` climbing. Since success is measured by decode-ability, not hash correctness, its score monotonically increases relative to a genuinely correct but less-frequently-successful peer, so `get_most_reliable_source` keeps selecting the liar for future requests inside the same `AttachmentsDownloader` (`reliability_reports` is keyed by peer URL and persists across batches, see `run()` at lines 115-124 [6](#0-5) ).

### Impact Explanation
This breaks the equality "reliability score reflects hash-correct byte delivery" that source selection depends on. The practical effect is a bounded, repeatable denial-of-service on BNS attachment resolution: a consensus-committed attachment that a legitimate peer actually holds can be starved because the downloader keeps preferring the higher-scored lying peer for the `/v2/attachments/{hash}` GET, causing the batch to keep failing and retry with exponential backoff (`AttachmentsBatch::bump_retry_count`) while the correct peer is comparatively under-selected. This matches the "High - bounded compute DoS on a read endpoint / attachment/BNS mismatch" category: no forged data is ever accepted into consensus state (final resolution still requires the decoded attachment to satisfy the tracked `content_hash` elsewhere in the pipeline), but resolution of real, valid BNS attachments can be persistently delayed or denied by an attacker who need only run an ordinary outbound-reachable peer.

### Likelihood Explanation
- Attacker only needs to run a normal Stacks peer that other nodes select as an outbound sync peer and can be queried via `/v2/attachments/inv` and `/v2/attachments/{hash}` — both public P2P/RPC-reachable endpoints, no secret or privileged role required.
- Cost per message is trivial: send any well-formed `Attachment` HTTP payload.
- The reliability score for that peer only grows (no `bump_failed_requests()` is ever triggered by content mismatch), so the advantage is durable across the life of the `AttachmentsDownloader`.
- Repeatable indefinitely as long as the attacker keeps responding to requests.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after decoding the response, compute the attachment's actual hash and compare it to `request.content_hash` before calling `report.bump_successful_requests()`. If the hash does not match, treat it as a failed/malicious response (`bump_failed_requests()`, and optionally mark the peer as faulty/deregister the event), and do not insert the attachment into `self.attachments`. This restores the invariant that reliability score reflects delivery of hash-correct bytes, not merely a well-formed HTTP response.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` mirroring `test_downloader_context_attachment_requests`:
1. Build an `AttachmentsBatchStateContext` with two peer sources for the same `content_hash`: peer A (initially highest score) and peer B (lower score).
2. Construct a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has peer A's response decode into an `Attachment` whose content does **not** hash to `content_hash`, and peer B's response decode into the correct `Attachment`.
3. Call `extend_with_attachments` and then inspect `context.peers[peer_A_url]` and `context.peers[peer_B_url]`.
4. Assert that peer A's `ReliabilityReport.total_requests_success` was incremented despite delivering wrong-hash content (demonstrating the flaw), and that the resulting `attachments` set does not exclude/deprioritize peer A on subsequent `get_most_reliable_source()` calls — showing the downloader would keep selecting peer A for further requests instead of deprioritizing it.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L115-124)
```rust
                let mut peers = HashMap::new();
                for peer in network.get_outbound_sync_peers() {
                    if let Some(peer_url) = network.get_data_url(&peer) {
                        let report = match self.reliability_reports.get(&peer_url) {
                            Some(report) => report.clone(),
                            None => ReliabilityReport::empty(),
                        };
                        peers.insert(peer_url, report);
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

**File:** stackslib/src/net/atlas/download.rs (L1073-1108)
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
```

**File:** stackslib/src/net/atlas/download.rs (L1110-1118)
```rust
    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".to_string(),
            format!("/v2/attachments/{}", &self.content_hash),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1299-1305)
```rust
    pub fn score(&self) -> u32 {
        let n = self.total_requests_sent;
        if n == 0 {
            return n;
        }
        self.total_requests_success * 1000 / (n * 1000) + n
    }
```

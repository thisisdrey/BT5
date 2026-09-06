### Title
Malformed-JSON HTTP 200 responses from a malicious peer are never penalized enough to exclude it from future attachment/inventory requests, and a scoring bug even rewards persistent failure - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`BatchedRequestsState::PollRequests` only marks a peer faulty on HTTP 404 or connection failure; any other status (including 200 with an undecodable body) is placed in `state.succeeded` and later fails to decode in `extend_with_attachments`/`extend_with_inventories`, which only calls `report.bump_failed_requests()` without ever removing the peer from `AttachmentsBatchStateContext::peers` or `AttachmentsDownloader::reliability_reports`. Compounding this, `ReliabilityReport::score()` has an integer-division bug that makes the score for an always-failing peer converge to the same value as a perfectly reliable peer (`n`), so a malicious, always-failing peer is never meaningfully deprioritized and can remain the top-ranked (and thus exclusively selected) source for attachment downloads indefinitely.

### Finding Description
The claimed broken equality holds: a degrading `ReliabilityReport` never implies exclusion. Tracing the exact path:

1. `BatchedRequestsState::PollRequests` (`stackslib/src/net/atlas/download.rs:899-910`) only inserts into `state.faulty_peers` for `status_code == 404` or a failed connection (`stackslib/src/net/atlas/download.rs:868-885`). Any other status (200 with garbage body, 500, 400, etc.) goes to `state.succeeded.insert(request, Some(response))`.
2. `extend_with_attachments`/`extend_with_inventories` (`stackslib/src/net/atlas/download.rs:490-558`) attempt `response.decode_atlas_get_attachment()` / `decode_atlas_attachments_inv_response()`; on decode failure they only call `report.bump_failed_requests()` (`stackslib/src/net/atlas/download.rs:521`, `:551`) and `continue` — the peer's `UrlString` key is never removed from `self.peers` (context) nor from `AttachmentsDownloader::reliability_reports` (`stackslib/src/net/atlas/download.rs:183-185`).
3. Each new `AttachmentsBatch` cycle rebuilds `peers` directly from `network.get_outbound_sync_peers()` (`stackslib/src/net/atlas/download.rs:115-124`), re-inserting the same malicious peer's `ReliabilityReport` from the persisted map — there is no score-based filtering anywhere in this file.
4. Worse, `ReliabilityReport::score()` (`stackslib/src/net/atlas/download.rs:1299-1305`) computes `total_requests_success * 1000 / (n * 1000) + n`. Because of the redundant `* 1000` on both sides, this expression collapses to `floor(success / n) + n`, which is `0 + n` for any peer that isn't perfectly successful. An always-failing malicious peer's score therefore grows as `n` (number of requests sent) exactly like a nearly-perfect peer's score grows as `n + 1`. Since `get_most_reliable_source` (`stackslib/src/net/atlas/download.rs:1074-1079`) and the `Ord` impls for `AttachmentRequest`/`AttachmentsInventoryRequest` (`stackslib/src/net/atlas/download.rs:1088-1096`, `:1016-1020`) rank purely by this score, a persistently-failing but frequently-queried peer can outrank a legitimate peer with fewer total interactions, causing all subsequent requests for that content hash to be routed to `get_url()` of the malicious peer only (single source per `AttachmentRequest`, no fan-out fallback).

### Impact Explanation
An attacker who completes a normal P2P handshake and advertises a data URL becomes part of `get_outbound_sync_peers()` and is included in every `AttachmentsBatch`. By returning HTTP 200 with a body that fails `serde_json::from_value`, the attacker's peer is never marked faulty/deregistered from future selection. Combined with the `score()` bug, the attacker's `ReliabilityReport` can outrank honest peers over time, causing `get_prioritized_attachments_requests`/`get_prioritized_attachments_inventory_requests` to route attachment/inventory downloads to the attacker on every retry until `max_attachment_retry_count` is exhausted and the batch is dropped (`stackslib/src/net/atlas/download.rs:188-205`). This is a bounded but repeatable compute-and-time DoS on attachment resolution, starving legitimate BNS attachment data from ever being resolved by the victim node while the attacker remains connected.

### Likelihood Explanation
Preconditions: attacker only needs to run an unprivileged peer that completes a handshake and is selected as an outbound sync peer with a reachable data URL — no secrets, no special role. Cost is a single always-on HTTP endpoint returning 200 with malformed JSON. The effect is repeatable across every `AttachmentsBatch` cycle for as long as the peer connection persists, since nothing in `AttachmentsDownloader`/`AttachmentsBatchStateContext` ever removes or caps a poorly-performing peer.

### Recommendation
- Fix `ReliabilityReport::score()` to not double-scale by 1000 (e.g., `total_requests_success * 1000 / n` without the extra `* 1000` in the denominator) so failure ratio is properly reflected.
- Treat non-2xx status codes and JSON-decode failures the same as 404/connection failures: insert into `faulty_peers`/deregister the event, and additionally apply a minimum reliability threshold (or exponential penalty) that excludes/deprioritizes peers with sustained decode failures from `get_prioritized_attachments_requests`/`get_prioritized_attachments_inventory_requests`, and periodically evict/reset chronically failing entries from `AttachmentsDownloader::reliability_reports`.

### Proof of Concept
Rust test plan in `stackslib/src/net/atlas/tests.rs` (or a new test module):
1. Stand up a mock peer whose HTTP handler for `/v2/attachments/<hash>` and `/v2/attachments/inv` always returns HTTP 200 with a body that is valid HTTP but not valid `GetAttachmentResponse`/`GetAttachmentsInvResponse` JSON (e.g., `{}`).
2. Construct an `AttachmentsDownloader` with this peer as the sole/highest-`n` outbound sync peer and drive `AttachmentsDownloader::run` across N `AttachmentsBatch` retry cycles (N > `max_attachment_retry_count`).
3. After each cycle assert: `report.bump_failed_requests()` was invoked (`total_requests_sent` increments) via `AttachmentsDownloader::reliability_reports.get(&peer_url)`.
4. Assert that `peer_url` is never absent from `context.peers` in a subsequent batch, and that `get_prioritized_attachments_requests()`/`get_prioritized_attachments_inventory_requests()` still returns the malicious peer as `get_most_reliable_source()` for a `content_hash` even after all N failures — i.e., `ReliabilityReport::score()` for the malicious peer is `>=` that of a hypothetical honest peer with fewer total requests, demonstrating the peer is never excluded. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** stackslib/src/net/atlas/download.rs (L490-528)
```rust
    pub fn extend_with_inventories(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentsInventoryRequest>,
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

            if let Ok(response) = response.decode_atlas_attachments_inv_response() {
                let peer_url = request.get_url().clone();
                match self.inventories.entry(request.key()) {
                    Entry::Occupied(responses) => {
                        responses.into_mut().insert(peer_url, response);
                    }
                    Entry::Vacant(v) => {
                        let mut responses = HashMap::new();
                        responses.insert(peer_url, response);
                        v.insert(responses);
                    }
                };
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

**File:** stackslib/src/net/atlas/download.rs (L887-910)
```rust
                            Some(ref mut convo) => {
                                match convo.try_get_response() {
                                    None => {
                                        // still waiting
                                        debug!(
                                            "Atlas: Request {} (event_id: {}) is still waiting for a response",
                                            request,
                                            event_id
                                        );
                                        pending_requests.insert(event_id, request);
                                        continue;
                                    }
                                    Some(response) => {
                                        let peer_url = request.get_url().clone();
                                        if response.preamble().status_code == 404 {
                                            state.faulty_peers.insert(event_id, peer_url);
                                            continue;
                                        }
                                        debug!(
                                            "Atlas: Request {} (event_id: {}) received HTTP 200",
                                            request, event_id
                                        );
                                        state.succeeded.insert(request, Some(response));
                                    }
```

**File:** stackslib/src/net/atlas/download.rs (L1088-1096)
```rust
impl Ord for AttachmentRequest {
    fn cmp(&self, other: &AttachmentRequest) -> Ordering {
        other.sources.len().cmp(&self.sources.len()).then_with(|| {
            let (_, report) = self.get_most_reliable_source();
            let (_, other_report) = other.get_most_reliable_source();
            report.cmp(other_report)
        })
    }
}
```

**File:** stackslib/src/net/atlas/download.rs (L1268-1306)
```rust
pub struct ReliabilityReport {
    pub total_requests_sent: u32,
    pub total_requests_success: u32,
}

impl ReliabilityReport {
    pub fn bump_successful_requests(&mut self) {
        self.total_requests_sent += 1;
        self.total_requests_success += 1;
    }

    pub fn bump_failed_requests(&mut self) {
        self.total_requests_sent += 1;
    }
}

impl ReliabilityReport {
    pub fn new(total_requests_sent: u32, total_requests_success: u32) -> ReliabilityReport {
        ReliabilityReport {
            total_requests_sent,
            total_requests_success,
        }
    }

    pub fn empty() -> ReliabilityReport {
        ReliabilityReport {
            total_requests_sent: 0,
            total_requests_success: 0,
        }
    }

    pub fn score(&self) -> u32 {
        let n = self.total_requests_sent;
        if n == 0 {
            return n;
        }
        self.total_requests_success * 1000 / (n * 1000) + n
    }
}
```

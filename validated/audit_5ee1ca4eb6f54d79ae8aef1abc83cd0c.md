### Title
Malformed AttachmentRequest response body never triggers peer deregistration - reliability-score-only penalty via `extend_with_attachments` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` treats a decode failure of `decode_atlas_get_attachment()` the same as any other soft failure: it merely calls `report.bump_failed_requests()` and continues, never adding the peer's event to `events_to_deregister`. Because `events_to_deregister` is only populated from `results.faulty_peers`, and `faulty_peers` is only populated at the transport layer (connection failure or HTTP 404) in `BatchedRequestsState::try_proceed`, a peer that returns HTTP 200 with a garbage/malformed JSON body is never deregistered, no matter how many times it does so.

### Finding Description
The relevant code path is: [1](#0-0) 

For each succeeded request/response pair, if `response.decode_atlas_get_attachment()` fails, the code falls into the `else` branch and only calls `report.bump_failed_requests()`; the loop simply `continue`s to the next request. `events_to_deregister` is populated only from `results.faulty_peers`, which is populated exclusively in `BatchedRequestsState::try_proceed` for connection failures and HTTP 404 responses: [2](#0-1) 

An HTTP 200 response whose body is malformed/unparseable JSON never reaches `faulty_peers` — it's inserted into `state.succeeded` at line 909 as soon as status is 200, and it is only in `extend_with_attachments` that the body is actually decoded. Thus the fault (bad payload) is detected too late in the pipeline (after `faulty_peers` has already been finalized for that batch), and the only consequence is a `bump_failed_requests()` call, which merely decrements the peer's computed `score()` via `ReliabilityReport`: [3](#0-2) 

This score is used only to *prefer* more reliable peers when multiple sources exist for the same attachment (`AttachmentRequest::get_most_reliable_source`, `Ord for AttachmentRequest`): [4](#0-3) 

But it does not exclude the peer from the peer set used to build future batches. Peers are re-derived every batch cycle from `network.get_outbound_sync_peers()` (an entirely separate mechanism unrelated to `AttachmentsDownloader`'s reliability reports), and a bad score never prevents a peer from being retained in `self.reliability_reports` and reused as a candidate source in `get_prioritized_attachments_requests`: [5](#0-4) 

So the claimed equality — "peer marked faulty/deregistered == peer actually returned unusable data" — is indeed broken: a peer returning `HTTP 200` with an empty/malformed body is never deregistered via `events_to_deregister`, only ever downgraded in an internal reliability score that does not remove it from consideration, and if it happens to be the *only* source for a given content hash (`sources.len() == 1`), it will keep being selected indefinitely.

### Impact Explanation
This is a resilience/availability degradation of the Atlas attachment-resolution subsystem (used to fetch BNS name-zonefile attachments), not a memory-safety or consensus-integrity bug. A malicious peer that is a P2P outbound sync peer can repeatedly return HTTP 200 with malformed attachment JSON bodies for `AttachmentRequest`s, causing repeated failed decodes without ever being kicked off the peer set. If it is the sole/most attractive source (e.g., the only one advertising the attachment in its inventory), attachment resolution for that content hash will keep failing/retrying against the same bad peer until the batch retry count is exhausted, and — even after exhaustion — the malicious peer remains in `reliability_reports`/`get_outbound_sync_peers()` for the next batch, since only p2p-level (connection failure/404) faults cause deregistration. This matches "attachment/BNS mismatch"-style degradation in that legitimate BNS attachment resolution can be persistently starved by a single malicious/faulty peer, though it is a data-unavailability/DoS-of-a-subsystem issue rather than a false-canonical-state or write-corruption issue.

### Likelihood Explanation
Preconditions: the attacker must be an outbound sync peer of the victim node (reachable via ordinary p2p handshake — no privileged role, secret, or admin access needed) and must be selected as (one of) the source(s) advertising the attachment in its `/v2/attachments/inv` response so that it is picked as a request target in `get_prioritized_attachments_requests`. The attacker's cost is trivial: respond to `/v2/attachments/{content_hash}` GET requests with HTTP 200 and a body that fails `decode_atlas_get_attachment()` (e.g., invalid JSON, wrong schema, oversized/garbage bytes as long as they pass whatever size/HTTP framing limits exist upstream). This is fully repeatable across arbitrarily many `AttachmentRequest` rounds/batches, since nothing in this code path ever adds the peer's event ID to `events_to_deregister`.

### Recommendation
In `extend_with_attachments` (and equivalently `extend_with_inventories`), when `decode_atlas_get_attachment()` (or `decode_atlas_attachments_inv_response()`) fails on a 200-status response, treat this as peer misbehavior equivalent to a 404/connection failure: insert the event id / peer URL into `results.faulty_peers` (or a new decode-failure set that also feeds `events_to_deregister`), not just decrement the reliability score. Optionally also apply a hard threshold on `ReliabilityReport::score()` to exclude repeatedly-failing peers from `get_prioritized_attachments_requests`/`get_prioritized_attachments_inventory_requests` regardless of deregistration.

### Proof of Concept
Add a unit test in `stackslib/src/net/atlas/download.rs` (or a new test module) that:
1. Constructs an `AttachmentsBatchStateContext` with a single peer `url` and an empty `ReliabilityReport`.
2. Builds a `BatchedRequestsResult<AttachmentRequest>` with `succeeded` containing one `AttachmentRequest` mapped to `Some(StacksHttpResponse)` whose body is a malformed JSON payload (such that `decode_atlas_get_attachment()` returns `Err`), and an empty `faulty_peers` map.
3. Calls `context.extend_with_attachments(&mut results)` `N` times (simulating `N` consecutive malformed responses across rounds).
4. Asserts `context.events_to_deregister.is_empty()` remains true after all `N` iterations, while `context.peers[&url].total_requests_sent == N` and `total_requests_success == 0` — demonstrating the peer is penalized only in score, never scheduled for deregistration, confirming the finding.

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

**File:** stackslib/src/net/atlas/download.rs (L899-910)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L1073-1096)
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
```

**File:** stackslib/src/net/atlas/download.rs (L1273-1306)
```rust
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

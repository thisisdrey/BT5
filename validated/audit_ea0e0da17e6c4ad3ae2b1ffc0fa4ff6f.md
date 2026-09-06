### Title
Attackable Atlas attachment reliability accounting lets an unprivileged peer poison BNS attachment resolution with false inventory bits that are never penalized - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
A remote peer that answers `GET /v2/attachments/inv` truthfully (HTTP 200) but always 404s on `GET /v2/attachments/{content_hash}` is selected as the preferred source for attachments it falsely advertises, and because the download state machine never records a failure for its own peer on a 404 response, its `ReliabilityReport` score is never degraded. This lets an unprivileged, remote, self-hosted peer permanently steer the victim's `AttachmentsBatch` retries toward itself for content it does not actually hold, exhausting `max_attachment_retry_count` before a truthful peer's copy is ever fetched.

### Finding Description
`RPCGetAttachmentsInvRequestHandler::try_handle_request` [1](#0-0)  simply echoes whatever `AtlasDB::get_attachments_available_at_page_index` returns from the *responding* peer's own local database. Because the attacker fully controls their own node/`AtlasDB`, they can set `AttachmentPage.inventory` bits to `1` (available) for `attachment_index` values with no backing content, with no server-side verification against actual stored attachment bytes.

On the victim's side, `AttachmentsBatchStateContext::get_prioritized_attachments_requests` [2](#0-1)  collects every peer whose inventory response has the bit set into `AttachmentRequest.sources`, and `AttachmentRequest::get_most_reliable_source`/`get_url` [3](#0-2)  selects only the single highest-`ReliabilityReport::score()` peer to actually query — it does not fall back to other sources in the same set if that peer fails.

The critical break is in `BatchedRequestsState::try_proceed` (PollRequests branch): on an HTTP 404 response, the request is recorded only into `state.faulty_peers` and then `continue`s, **never** being inserted into `state.succeeded` [4](#0-3) . `AttachmentsBatchStateContext::extend_with_attachments` only calls `report.bump_failed_requests()` for entries actually present in `results.succeeded.drain()` [5](#0-4) , so a 404 (the exact response a lying peer gives) never decrements that peer's reliability. Meanwhile `ReliabilityReport::score()` [6](#0-5)  is dominated by `total_requests_sent` (`n`), which keeps growing every time the peer answers a `/v2/attachments/inv` request truthfully (a request the attacker can always satisfy cheaply). So the malicious peer's score can only go up, never down for its lies, while a fresh/legitimate peer with fewer total interactions scores lower and is never chosen as long as the malicious peer's `sources` entry is present.

Each retry cycle re-derives `AttachmentsBatchStateContext` from scratch via `AttachmentsDownloader::run`, re-polling all peers' inventories and re-computing `get_prioritized_attachments_requests`, so the same biased selection recurs identically on every one of the `max_attachment_retry_count` retries (`AttachmentsBatch::bump_retry_count` / retry gating) [7](#0-6) , after which the batch is dropped entirely [8](#0-7) .

### Impact Explanation
This lets an unprivileged remote peer permanently prevent a victim node from resolving a specific BNS-attached attachment even when a truthful peer holds the real content, because the download state machine's source-selection is bindable to a liar whose reliability score can never fall due to the false-positive/no-penalty bug on 404. The attack is entirely repeatable (attacker just keeps answering `/v2/attachments/inv` truthfully while returning 404 on the actual content) and does not require any privileged role, secret, or write access — matching the "steering a node off the tip via false inventory / attachment-BNS mismatch" High-severity category.

### Likelihood Explanation
Preconditions are modest and achievable by any unprivileged remote actor: run a normal Stacks peer that becomes one of the victim's `outbound_sync_peers` (ordinary peer gossip/handshake, no special role), maintain a locally-controlled `AtlasDB` where `is_available` bits are set for chosen `attachment_index` values without real content, and always answer `/v2/attachments/inv` (cheap) while 404ing `/v2/attachments/{content_hash}`. Because reliability score is dominated by request volume and never penalized for the attachment-request failure path, the attacker can trivially reach or exceed a fresh honest peer's score. No timing races, no cryptographic bypass, and no config secret are required.

### Recommendation
- In `BatchedRequestsState::try_proceed`, treat 404 responses for `AttachmentRequest` the same as other failures: route them through `results.succeeded.insert(request, None)` (or an equivalent path) so `extend_with_attachments` calls `report.bump_failed_requests()` for the peer that lied.
- Change `AttachmentRequest::get_url`/selection logic to fall back to the next-best source in `sources` within the same round when the top choice 404s, instead of dropping the attachment for the whole retry cycle.
- Reconsider `ReliabilityReport::score()` so that success ratio, not just `n`, dominates ranking (e.g., weight failed requests negatively rather than integer-dividing to near-zero contribution).

### Proof of Concept
Add a `stackslib::net::atlas` test that:
1. Builds an `AttachmentsBatchStateContext` with two peers: `honest` (a `ReliabilityReport` with low `n` but successful past attachment serving) and `liar` (higher `n` purely from inv-request successes).
2. Feeds `extend_with_inventories` a `GetAttachmentsInvResponse` where both peers report `inventory=[1,...]` for the target `attachment_index`, then call `get_prioritized_attachments_requests` and assert `AttachmentRequest::get_url()` returns `liar`'s URL because of its higher `score()`.
3. Simulate `BatchedRequestsResult` for the `AttachmentRequest` state with the `liar`'s response marked as a 404 (i.e., inserted only into `faulty_peers`, not `succeeded`), call `extend_with_attachments`, and assert `context.peers.get(liar_url).unwrap()` is **unchanged** (`total_requests_sent`/`total_requests_success` not incremented), proving no reliability penalty was applied.
4. Repeat the full `AttachmentsDownloader::run` cycle up to `connection_options.max_attachment_retry_count` times with a mocked `PeerNetwork`/`DNSClient`, and assert the target `content_hash` never appears in `resolved_attachments` even though `honest` could have served it, confirming the batch is dropped after retries exhausted while the true content was never fetched.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L179-196)
```rust
        for page_index in page_indexes.iter() {
            let page_res =
                node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                    match network
                        .get_atlasdb()
                        .get_attachments_available_at_page_index(*page_index, &index_block_hash)
                    {
                        Ok(inventory) => Ok(AttachmentPage {
                            inventory,
                            index: *page_index,
                        }),
                        Err(e) => {
                            let msg = format!("Unable to read Atlas DB - {}", e);
                            warn!("{}", msg);
                            Err(msg)
                        }
                    }
                });
```

**File:** stackslib/src/net/atlas/download.rs (L183-205)
```rust
                for (peer_url, report) in context.peers.drain() {
                    self.reliability_reports.insert(peer_url, report);
                }

                // Re-insert AttachmentsBatch back to the queue if not fully processed
                if !context.attachments_batch.has_fully_succeed() {
                    context.attachments_batch.bump_retry_count();
                    // If max_attachment_retry_count not reached, we'll re-enqueue the batch
                    if context.attachments_batch.retry_count
                        < context.connection_options.max_attachment_retry_count
                    {
                        info!(
                            "Atlas: re-enqueuing batch {:?} for retry",
                            context.attachments_batch
                        );
                        self.priority_queue.push(context.attachments_batch.clone());
                    } else {
                        info!(
                            "Atlas: dropping batch {:?} retries count exceeded",
                            context.attachments_batch
                        );
                    }
                }
```

**File:** stackslib/src/net/atlas/download.rs (L404-459)
```rust
    pub fn get_prioritized_attachments_requests(&self) -> BinaryHeap<AttachmentRequest> {
        let mut queue = BinaryHeap::new();
        let mut enqueued = HashSet::new();
        for ((contract_id, pages, _), peers_responses) in self.inventories.iter() {
            let missing_attachments = match self
                .attachments_batch
                .attachments_instances
                .get(contract_id)
            {
                None => continue,
                Some(missing_attachments) => missing_attachments,
            };
            // Note: we're getting missing_attachments (attachment_id: content_hash)
            for (attachment_index, content_hash) in missing_attachments.iter() {
                let page_index = attachment_index / AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;
                // Since there's a limit in the number of pages that a node can request,
                // we can potentially have multiple inventory request at once.
                if !pages.contains(&page_index) {
                    continue;
                }

                if enqueued.contains(content_hash) {
                    debug!("Atlas: {} already enqueued", content_hash);
                    continue;
                }

                let mut sources = HashMap::new();
                let position_in_page =
                    attachment_index % AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;

                for (peer_url, response) in peers_responses.iter() {
                    // Considering the response, look for the page with the index
                    // we're looking for.
                    let search_page = response.pages.iter().find(|page| page.index == page_index);

                    let has_attachment = search_page
                        .and_then(|search_page| {
                            search_page.inventory.get(position_in_page as usize)
                        })
                        .map(|result| *result == 1)
                        .unwrap_or(false);

                    if !has_attachment {
                        debug!(
                            "Atlas: peer does not have attachment ({}, {}) in its inventory {:?}",
                            page_index, position_in_page, response.pages
                        );
                        continue;
                    }

                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
                }
```

**File:** stackslib/src/net/atlas/download.rs (L530-553)
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
```

**File:** stackslib/src/net/atlas/download.rs (L899-904)
```rust
                                    Some(response) => {
                                        let peer_url = request.get_url().clone();
                                        if response.preamble().status_code == 404 {
                                            state.faulty_peers.insert(event_id, peer_url);
                                            continue;
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

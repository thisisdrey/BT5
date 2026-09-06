### Title
Poisoned Atlas attachment inventory bit + inverted `ReliabilityReport::score()` causes permanent BNS attachment starvation - ([File: stackslib/src/net/atlas/download.rs])

### Summary
A remote peer can answer `GetAttachmentsInvResponse` with a forged `AttachmentPage.inventory` bit claiming to hold an attachment it does not serve; `AttachmentsBatchStateContext::get_prioritized_attachments_requests()` accepts this claim on trust and adds the lying peer to `sources`. Because `ReliabilityReport::score()` is computed such that failed-request volume dominates the score (rather than success ratio), the lying peer becomes and *stays* the "most reliable source" across retries, causing the batch's `retry_count` to hit `max_attachment_retry_count` and be permanently dropped even though a genuine provider exists.

### Finding Description
`get_prioritized_attachments_requests()` builds the `sources` map for an `AttachmentRequest` purely by trusting the remote inventory bit: [1](#0-0) 
No proof of possession is required — a peer only needs to set `inventory[position_in_page] = 1` in its `GetAttachmentsInvResponse` (decoded via `decode_atlas_attachments_inv_response`, stored unchecked in `self.inventories` in `extend_with_inventories`) to be added as a "source" for an attachment it does not actually hold. [2](#0-1) 

When an `AttachmentRequest` is later dispatched, only a single URL is used per attempt — the one with the highest `ReliabilityReport` score, chosen by `get_most_reliable_source()`: [3](#0-2) 

The root cause that turns this into a *permanent* starvation (not just a self-healing minor slowdown) is a logic bug in `ReliabilityReport::score()`: [4](#0-3) 
`total_requests_success * 1000 / (n * 1000)` is arithmetically identical to `total_requests_success / n` (integer division), which truncates to `0` for any partial success ratio and to `1` only when `success == n`. This means the score reduces to essentially `n` (number of attempts, mostly regardless of success), so a peer that has been tried and **failed** several times (`bump_failed_requests` only increments `total_requests_sent`) ends up with a *higher* score than a never-tried, honest peer whose report is still `ReliabilityReport::empty()` (score `0`). Concretely: malicious peer after 1 failed attempt has `n=1, success=0 → score = 0 + 1 = 1`, beating an untried honest peer's score of `0`.

Exploit flow:
1. Attacker peer advertises inventory bit `1` for `attachment_index` X in its `GetAttachmentsInvResponse`, even though it does not serve the content.
2. `get_prioritized_attachments_requests()` includes attacker's URL in `sources` for X's `AttachmentRequest` (per the cited code, `has_attachment` becomes true from the forged bit alone).
3. `get_most_reliable_source()` initially ties or is arbitrary among untried peers; once attacker is picked and fails (404/timeout), its score becomes `>0` due to the buggy formula, guaranteeing it is picked again on every subsequent retry within `AttachmentsDownloader::run`.
4. Each retry the honest peer serving X is never selected (or rarely), so `context.attachments_batch` never resolves X; `bump_retry_count()` increments `retry_count`.
5. Once `retry_count >= connection_options.max_attachment_retry_count`, the batch is dropped permanently: [5](#0-4) 

This breaks the invariant `peer_claims_has(attachment) == peer_actually_serves(attachment)` and combines with the reliability-scoring defect to make the DoS durable rather than transient.

### Impact Explanation
An unprivileged remote peer that establishes an outbound-sync relationship (or is selected as an outbound sync peer) can permanently prevent a node from resolving a specific, on-chain-committed BNS/Atlas attachment, even though a legitimate provider exists elsewhere in the network. This is a targeted availability/inventory-poisoning DoS on name resolution data, matching the "attachment/BNS mismatch"/false-inventory steering category of High impact. It is repeatable for any attachment index the attacker chooses to claim falsely, and does not require sending large volumes of traffic — a single crafted `GetAttachmentsInvResponse` plus refusing/failing the follow-up content request is sufficient to poison scoring for a given batch cycle.

### Likelihood Explanation
Preconditions: attacker must be one of the node's outbound sync peers (`network.get_outbound_sync_peers()`), which is achievable by any remote node that peers with the target and has an advertised data URL — no privileged role, secret, or slot ownership required. Attack cost is a single crafted HTTP-ish inventory response; no bandwidth flooding is needed. The bug is deterministic given the `score()` formula and requires no race condition; it will reproduce identically on every affected node running an outbound sync against the attacker.

### Recommendation
1. Fix `ReliabilityReport::score()` so that failed requests never outweigh a peer that has zero requests, and so that success ratio (not raw attempt count) dominates ranking — e.g. weight unproven peers and low-success peers correctly, or scale success ratio properly (e.g. `success * 1_000_000 / n` without adding `n`).
2. Do not rely on a single "most reliable" peer per attempt; either round-robin across all `sources` claiming to have the attachment, or fall back to less-recently-tried sources when the top-scored source repeatedly fails within the same batch.
3. Consider requiring a permanent negative mark (blacklisting for the current batch) for a peer's URL immediately after a single failed/404 fetch for a specific `content_hash`, rather than letting it remain eligible via a corrupted score.

### Proof of Concept
Add a variant to `stackslib/src/net/atlas/tests.rs::test_downloader_context_attachment_requests` (or a new test) that:
1. Constructs an `AttachmentsBatchStateContext` with two peers: `honest_url` and `attacker_url`.
2. Populates `context.inventories` for the relevant `(contract_id, pages, index_block_hash)` key with two `GetAttachmentsInvResponse` entries: `attacker_url` has `AttachmentPage.inventory[position_in_page] = 1` for an index it never actually serves; `honest_url` has the same bit set truthfully.
3. Calls `get_prioritized_attachments_requests()` and asserts the resulting `AttachmentRequest.sources` contains both URLs (`sources.len() == 2`), confirming the forged bit is accepted at face value.
4. Simulates repeated `run()` cycles where a request to `attacker_url` always fails (mock 404/no-response) and manually drives `ReliabilityReport::bump_failed_requests()` on `attacker_url`'s report while `honest_url`'s report stays at `ReliabilityReport::empty()`.
5. Asserts that after one failed round, `attacker_report.score() > honest_report.score()` (e.g. `1 > 0`), demonstrating `get_most_reliable_source()` will keep selecting `attacker_url`.
6. Drives `AttachmentsDownloader::run()` in a loop until `context.attachments_batch.retry_count >= connection_options.max_attachment_retry_count`, and asserts the batch is dropped (not re-enqueued in `priority_queue`) while the attachment content was never resolved, despite `honest_url` genuinely serving it.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L187-205)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L437-452)
```rust
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
```

**File:** stackslib/src/net/atlas/download.rs (L490-522)
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

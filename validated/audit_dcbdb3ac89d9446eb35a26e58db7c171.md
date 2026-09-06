### Title
Broken `ReliabilityReport::score()` formula lets an attacker peer dominate `AttachmentRequest::get_most_reliable_source`, causing real attachment fetches to be steered to a malicious/lying peer - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`ReliabilityReport::score()` is supposed to represent "how reliable is this peer", but due to an integer-division artifact it actually reduces almost entirely to `total_requests_sent` regardless of success ratio. This lets any peer that simply exchanges many attachment-inv/attachment requests with a node - success or failure - accumulate a higher score than a peer that has answered fewer requests with 100% correctness, letting `AttachmentRequest::get_most_reliable_source` (and thus `Requestable::get_url`/`make_request_type`) preferentially route real attachment fetches to that peer.

### Finding Description
`ReliabilityReport::score()` is defined as: [1](#0-0) 

Because both numerator and denominator are scaled by the same constant `1000`, `total_requests_success * 1000 / (n * 1000)` is mathematically identical to `floor(total_requests_success / n)`, which is `0` unless `total_requests_success == n` (perfect record), in which case it is `1`. So:

```
score(report) ≈ n + (1 if success == n else 0)
```

The score is therefore dominated entirely by `total_requests_sent` (`n`), not by the actual success ratio. Both `bump_successful_requests` and `bump_failed_requests` increment `total_requests_sent` by the same amount: [2](#0-1) 

This means an attacker peer that is merely willing to participate in many attachments-inv/attachment request/response rounds (garbage-but-decodable data bumps the success counter and preserves the "+1" bonus; even outright failing/garbage-that-fails-to-decode still bumps `n` via `bump_failed_requests`) can drive its `n` arbitrarily high and thereby outscore a genuinely correct peer that has simply had fewer request/response rounds with the node.

`AttachmentRequest::get_most_reliable_source` selects strictly by `v.score()`: [3](#0-2) 

and `Requestable::get_url`/`make_request_type` for `AttachmentRequest` use that selection to pick which peer actually receives the real `/v2/attachments/{content_hash}` request: [4](#0-3) 

The reports feeding into `AttachmentRequest.sources` are populated in `get_prioritized_attachments_requests`, keyed by whichever peers advertised (in their self-reported inventory) that they have the attachment - an attacker can freely claim to have any attachment in its `/v2/attachments/inv` response regardless of truth: [5](#0-4) [6](#0-5) 

So the broken equality claimed in the question - `selected_source_reliability == genuine_correct_response_rate` - does not hold: `selected_source_reliability` is actually proportional to raw traffic volume with the node, not to correctness. `Ord for AttachmentRequest` further amplifies the effect by tie-breaking request priority using the same broken `get_most_reliable_source` comparison: [7](#0-6) 

### Impact Explanation
An attacker peer that is (or becomes) part of `network.get_outbound_sync_peers()` can, over time, inflate its `ReliabilityReport` score purely by volume of interaction and false inventory claims, causing the node's Atlas attachment downloader to prefer it as the fetch target for genuinely-missing attachments. Because the peer can lie about having an attachment in its inventory and then return garbage/undecodable/incorrect content for the actual `/v2/attachments/{hash}` request, real attachment resolution for the affected `content_hash`(es) is denied while the attacker remains the top-ranked source, consuming the batch's `max_attachment_retry_count` before the batch is dropped: [8](#0-7) 

This is a node-local availability degradation of BNS zonefile/attachment resolution for names whose data routes through the poisoned peer - not a crash, forged canonical state, or unauthorized write. It only affects the local requester's view of attachment availability and is fully mitigated once the affected attachment is dropped and later requested from other peers, since `sources` are recomputed per `AttachmentRequest` construction based on current inventories/reports, not permanently pinned.

### Likelihood Explanation
Preconditions: the attacker must be a peer the node syncs with (in `outbound_sync_peers`), which is achievable by any unprivileged party running their own node and getting gossiped into the target's peer/neighbor set - no privileged role or secret required. The attacker only needs to answer inv/attachment requests over time to build up `total_requests_sent`; cost is proportional to the number of request/response rounds needed to reach a competitive `n`, which is cheap since each round is a normal-sized HTTP response. Repeatable indefinitely as long as the attacker remains a synced peer.

### Recommendation
Fix `ReliabilityReport::score()` to actually reflect the success ratio (e.g., a scaled fixed-point ratio like `total_requests_success * SCALE / total_requests_sent`, or a Wilson/Laplace-smoothed success rate), rather than an expression that collapses to `n`. Additionally, consider validating fetched attachment content against the requested `content_hash` before crediting a `bump_successful_requests`, and penalizing peers whose inventory claims ("has attachment") are not corroborated by a subsequent successful, hash-matching fetch.

### Proof of Concept
Add a unit test in `stackslib/src/net/atlas/tests.rs` (or `download.rs` test module) that:
1. Constructs `ReliabilityReport::new(sent, success)` for peer A: genuine peer with e.g. `sent=5, success=5` (perfect).
2. Constructs peer B (attacker): `sent=1000, success=1` (mostly garbage/failed but occasionally decodable), or even `sent=1000, success=0` (all failures).
3. Asserts `peer_b.score() > peer_a.score()` (per the broken formula, `1000 > 6`), demonstrating volume dominates correctness.
4. Builds an `AttachmentRequest` with `sources` containing both peers' `UrlString -> ReliabilityReport`, and asserts `get_most_reliable_source()` returns peer B's URL despite peer B's near-zero actual success rate:
```rust
let mut sources = HashMap::new();
sources.insert(UrlString::try_from("http://genuine-peer".to_string()).unwrap(), ReliabilityReport::new(5, 5));
sources.insert(UrlString::try_from("http://attacker-peer".to_string()).unwrap(), ReliabilityReport::new(1000, 1));
let req = AttachmentRequest { sources, content_hash: Hash160::from_data(b"x"), stacks_block_height: 1, canonical_stacks_tip_height: None };
let (url, _) = req.get_most_reliable_source();
assert_eq!(url.as_str(), "http://attacker-peer");
```
This directly exercises `AttachmentRequest::get_most_reliable_source` at [3](#0-2)  and `ReliabilityReport::score` at [1](#0-0) .

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

**File:** stackslib/src/net/atlas/download.rs (L434-459)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L466-475)
```rust
                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
            }
```

**File:** stackslib/src/net/atlas/download.rs (L1073-1080)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
    }
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

**File:** stackslib/src/net/atlas/download.rs (L1104-1119)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L1273-1282)
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

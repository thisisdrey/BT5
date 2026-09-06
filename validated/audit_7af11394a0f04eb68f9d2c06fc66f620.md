### Title
Missing content-hash verification after attachment download allows a peer with a manipulated `ReliabilityReport` score to serve mismatched BNS attachment data - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentRequest::get_most_reliable_source` picks a download source purely by `ReliabilityReport::score()`, with no cryptographic binding to `content_hash`, so a peer that inflates its own reliability score is preferentially selected as the URL to fetch from. Critically, once that peer responds, `AttachmentsBatchStateContext::extend_with_attachments` decodes the HTTP response and inserts the returned `Attachment` directly into the accepted set without ever comparing its content hash to the expected `content_hash` of the request.

### Finding Description
`get_most_reliable_source` selects the (url, report) pair with the maximum `v.score()`: [1](#0-0) 

This selection is used both to order `AttachmentRequest`s in the priority queue and to determine the actual URL to fetch from via `Requestable::get_url`: [2](#0-1) 

Sources are populated per-peer from whichever peers claimed (via their inventory response) to have the attachment, each carrying whatever `ReliabilityReport` the local node currently has recorded for that peer: [3](#0-2) 

A `ReliabilityReport`'s score is driven purely by call counts (`bump_successful_requests` / `bump_failed_requests`) recorded from prior interactions such as inventory responses: [4](#0-3) 

A peer that answers many cheap, correctly-formed (but irrelevant) `/v2/attachments/inv` requests quickly accumulates `bump_successful_requests()` calls and raises its `score()` with no relation to whether it will faithfully serve the specific `content_hash` being requested. Because `max_by_key` has no tie-breaking or authentication logic tied to `content_hash`, an attacker-controlled peer with an inflated score is chosen over an honest peer that happens to have a lower/default score.

Once selected, the actual attachment payload is fetched and processed in `extend_with_attachments`, which decodes the HTTP response and unconditionally inserts the resulting `Attachment` into `self.attachments` — there is no check that the decoded attachment's content hash matches `request.content_hash`: [5](#0-4) 

So the combination is: (1) `get_most_reliable_source` provides no authentication that the chosen peer will serve correct bytes for `content_hash` — it is purely a reputation heuristic based on unrelated past traffic; and (2) the consumer of the response performs no post-hoc verification that the returned attachment's hash equals `content_hash` before accepting it into the resolved-attachments set. This means an attacker who (a) gets included as a source for the target `content_hash` (by advertising it in `/v2/attachments/inv`) and (b) has inflated their `ReliabilityReport` score via cheap successful inventory responses, can be chosen as the download source and then return a mismatched attachment blob for that `content_hash`, which is accepted as if it were the canonical committed attachment for that hash.

### Impact Explanation
This allows an unprivileged remote peer to have forged/incorrect BNS attachment content accepted and stored by a victim node as if it corresponded to the canonical `content_hash` referenced on-chain, i.e., attachment/BNS content-hash mismatch is not detected. This matches the "High: serving non-canonical state as canonical / attachment/BNS mismatch" impact category. It is repeatable per attachment/content hash and does not require any privileged role, RPC secret, or slot ownership — only running an ordinary peer that participates in the Atlas attachment-inventory/download protocol.

### Likelihood Explanation
Preconditions: the attacker must run a reachable peer that (1) is included as an outbound sync peer or otherwise gets its reliability report tracked, (2) responds to `/v2/attachments/inv` requests advertising it has the target attachment, and (3) has accumulated `bump_successful_requests()` calls (trivially done by answering multiple cheap inventory requests correctly) so its `score()` exceeds the honest peer's. All of this is achievable by any remote party who can run a normal Stacks peer node — no admin role, secret, or privileged access is needed. Attacker cost is low (just serve correct inventory responses to raise score, then a single bad `/v2/attachments/{hash}` response), and the exploit is repeatable for any attachment the attacker wants to poison.

### Recommendation
Add an explicit content-hash check in `extend_with_attachments` before inserting the decoded attachment: compute `Hash160` over the returned attachment content and compare it against `request.content_hash`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and do not insert into `self.attachments`. Additionally, consider not relying solely on `score()` for source selection when the request is content-addressed (`content_hash`) — since correctness can be verified post-hoc, the primary protection should be the hash check, but score-based selection should not be treated as an authentication mechanism.

### Proof of Concept
1. Construct a `ReliabilityReport` for an "attacker" peer URL with a high score (e.g., via repeated `bump_successful_requests()` calls) and a `ReliabilityReport` for an "honest" peer with default/lower score.
2. Build an `AttachmentRequest { content_hash, sources: {attacker_url: attacker_report, honest_url: honest_report}, .. }` and call `get_most_reliable_source()` — assert it returns `attacker_url`.
3. Simulate `extend_with_attachments` by constructing a `BatchedRequestsResult` where the "succeeded" entry for this request contains a `StacksHttpResponse` whose decoded `Attachment` content hashes to a value different from `request.content_hash`.
4. Call `AttachmentsBatchStateContext::extend_with_attachments` and assert that `context.attachments` now contains the mismatched attachment (i.e., no `Err`/rejection occurs), demonstrating the missing equality check `decoded_attachment.hash() == request.content_hash` at stackslib/src/net/atlas/download.rs:547-552.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L430-458)
```rust
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
```

**File:** stackslib/src/net/atlas/download.rs (L490-523)
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

**File:** stackslib/src/net/atlas/download.rs (L1104-1108)
```rust
impl Requestable for AttachmentRequest {
    fn get_url(&self) -> &UrlString {
        let (url, _) = self.get_most_reliable_source();
        url
    }
```

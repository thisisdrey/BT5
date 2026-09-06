### Title
Unverified attachment content lets a malicious peer inflate its `ReliabilityReport` and monopolize/DoS attachment resolution - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` credits a peer with a successful request (`report.bump_successful_requests()`) purely because a `GET /v2/attachments/{content_hash}` response decodes as valid JSON, without ever checking that the returned `Attachment`'s real hash equals the requested `content_hash`. Combined with `get_prioritized_attachments_requests`, which trusts unauthenticated inventory bits to build the candidate `sources` set and `AttachmentRequest::get_most_reliable_source`/`Ord`, which route future downloads to the highest-scoring peer, a lying peer can repeatedly "win" the routing for a given attachment while never delivering correct bytes, artificially raising its score and starving that attachment/BNS record of resolution.

### Finding Description
In `stackslib/src/net/atlas/download.rs`, `get_prioritized_attachments_requests` builds the `sources` map for an `AttachmentRequest` purely from unauthenticated `AttachmentPage.inventory` bits returned by `/v2/attachments/inv`, with no cross-check against the peer's actual ability to serve the content: [1](#0-0) 

`AttachmentRequest::get_most_reliable_source` and its `Ord` impl select/prioritize strictly by `ReliabilityReport::score()`, i.e., a peer that has answered "successfully" many times outranks others regardless of correctness: [2](#0-1) 

The actual bug is in `extend_with_attachments`: it decodes the HTTP response via `decode_atlas_get_attachment()` and, if decoding succeeds, unconditionally calls `report.bump_successful_requests()` and inserts the attachment into the batch's attachment set — there is **no comparison of `response.attachment.hash()` against `request.content_hash`** anywhere in this function: [3](#0-2) 

`decode_atlas_get_attachment` / `GetAttachmentResponse::deserialize` only hex-decode the payload into an `Attachment { content }`; no hash validation occurs there either: [4](#0-3) [5](#0-4) 

Because the score-bump has no hash check, a peer can answer any `AttachmentRequest{content_hash}` with arbitrary well-formed hex content and still be scored as "successful," regardless of what it actually served.

Note: downstream, `run()` resolves attachment instances by recomputing the real hash of whatever bytes were returned (`attachment.hash()`), and DB writes/lookups (`insert_instantiated_attachment`, `find_all_attachment_instances`) key off that recomputed hash, not the requested `content_hash`: [6](#0-5) [7](#0-6)  This means a malicious peer's mismatched bytes cannot be stored/served as if they were the correct content for the target `content_hash` (finding a Hash160 preimage is computationally infeasible), so the specific claim "served-bytes-hash == content_hash is broken and forged data is served as canonical" does **not** hold — the equality is preserved by construction at the storage layer. The real, exploitable defect is narrower: the reliability-scoring path itself is unauthenticated, letting an attacker inflate its score and monopolize routing for a target attachment while never resolving it, which starves that attachment/BNS record until `max_attachment_retry_count` is exhausted and the batch is dropped ("Atlas: dropping batch ... retries count exceeded").

### Impact Explanation
An unprivileged remote peer can: (1) falsely set inventory bits for content it cannot serve, getting itself included as a source for a target `content_hash`; (2) once selected as `get_most_reliable_source`, respond to `/v2/attachments/{content_hash}` with arbitrary decodable-but-wrong content; (3) have its `ReliabilityReport.total_requests_success` bumped regardless of correctness, which increases `score()` and biases future `Ord`/routing decisions in its favor for this and other attachments. Repeated over the bounded retry count, this denies resolution of a specific attachment/BNS record even when honest peers could serve it — a targeted, repeatable denial-of-service on Atlas/BNS attachment resolution, without ever causing forged bytes to be accepted as the canonical attachment (DB storage/resolution keys off the recomputed real hash).

### Likelihood Explanation
No privileged role, secret, or the tip's canonical state is required — only that the node has attachments to resolve and includes the attacker as an outbound sync peer / data-URL source, which is standard Atlas peer discovery. The attacker cost is a handful of well-formed HTTP responses to `/v2/attachments/inv` and `/v2/attachments/{hash}`; each malicious response is credited as "successful" with zero verification cost to the node, making the reliability-inflation cheap and repeatable.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs`), verify `response.attachment.hash() == request.content_hash` before calling `report.bump_successful_requests()` and before inserting into `self.attachments`; on mismatch, call `report.bump_failed_requests()` (and consider treating repeated mismatches as cause to deregister/penalize the peer, similar to `faulty_peers` handling) so that `ReliabilityReport.score()` cannot be inflated by serving incorrect content.

### Proof of Concept
Add to `stackslib/src/net/atlas/tests.rs`:
1. Build an `AttachmentsBatchStateContext` with one attachment instance whose `content_hash` = `H(correct_bytes)`.
2. Simulate inventory responses so peer `evil` (score 0/0) claims it has the attachment (`inventory = [1]`), and construct the resulting `AttachmentRequest` via `get_prioritized_attachments_requests`.
3. Feed a `BatchedRequestsResult<AttachmentRequest>::succeeded` entry for `evil` containing a `StacksHttpResponse` decoding to `Attachment { content: wrong_bytes }` (`wrong_bytes.hash() != content_hash`).
4. Call `context.extend_with_attachments(&mut results)` and assert that `context.peers.get(&evil_url).unwrap().total_requests_success == 0` (fails today because current code bumps it to `1`) — demonstrating the score-inflation bug; separately assert that `attachment.hash() != content_hash` never gets resolved into `attachment_instances` (confirming the storage-layer equality itself is not broken).

### Citations

**File:** stackslib/src/net/atlas/download.rs (L153-169)
```rust
            AttachmentsBatchStateMachine::Done(ref mut context) => {
                for attachment in context.attachments.drain() {
                    let attachments_instances = network
                        .atlasdb
                        .find_all_attachment_instances(&attachment.hash())
                        .map_err(net_error::DBError)?;
                    network
                        .atlasdb
                        .insert_instantiated_attachment(&attachment)
                        .map_err(net_error::DBError)?;
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
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

**File:** stackslib/src/net/atlas/mod.rs (L69-77)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
}
```

**File:** stackslib/src/net/api/getattachment.rs (L158-166)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
}
```

**File:** stackslib/src/net/atlas/db.rs (L576-592)
```rust
    pub fn insert_instantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let tx = self.tx_begin()?;
        tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)",
            params![attachment.hash(), attachment.content, now],
        )?;
        tx.execute(
            "UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1 AND status = ?2",
            params![attachment.hash(), AttachmentInstanceStatus::Checked],
        )?;
        tx.commit()?;
        Ok(())
    }
```

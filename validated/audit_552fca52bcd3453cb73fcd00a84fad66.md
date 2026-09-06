### Title
Attachment content is never validated against the requested `content_hash`, letting a malicious peer serve unrelated data that gets stored while the real committed attachment starves - (File: stackslib/src/net/atlas/download.rs, stackslib/src/net/api/getattachment.rs)

### Summary
`AttachmentRequest.content_hash` is the on-chain-committed hash the downloader is trying to satisfy, but `StacksHttpResponse::decode_atlas_get_attachment` [1](#0-0)  never checks the returned `Attachment` against it. `AttachmentsBatchStateContext::extend_with_attachments` blindly inserts whatever `Attachment` the peer returned into `self.attachments` and marks the request as a "successful" request for reliability-scoring purposes, with no comparison to `request.content_hash` [2](#0-1) . Downstream, resolution is keyed by `attachment.hash()` (the hash of whatever bytes the peer sent) instead of the originally requested `content_hash` [3](#0-2) .

### Finding Description
The broken invariant is: `resolved_attachment_hash == requested_content_hash` for every completed `AttachmentRequest`. This invariant is never enforced.

Path:
1. `AttachmentsBatchStateContext::get_prioritized_attachments_requests` builds an `AttachmentRequest{ content_hash, sources, ... }` for a real, on-chain-committed `content_hash`, choosing any peer that claims (via inventory) to have it as a `source` [4](#0-3) . Any remote peer that returns a positive bit in its `GetAttachmentsInvResponse` inventory page can become a listed source with no proof it actually possesses the correct content.
2. The request is dispatched over HTTP GET `/v2/attachments/<hash>` and the response body is parsed purely as JSON via `parse_json`/`decode_atlas_get_attachment`, which only decodes hex bytes into an `Attachment::new(bytes)` — no hash comparison against the requested/path hash is performed [5](#0-4) .
3. `extend_with_attachments` takes this decoded `Attachment` and inserts it into `context.attachments`, and calls `report.bump_successful_requests()` — treating the malicious/incorrect payload as a successful, reliable response [6](#0-5) .
4. In `AttachmentsDownloader::run`, for each drained attachment, the code computes `attachment.hash()` from the (possibly forged) bytes, looks up `find_all_attachment_instances(&attachment.hash())`, calls `insert_instantiated_attachment(&attachment)`, and calls `context.attachments_batch.resolve_attachment(&attachment.hash())` [3](#0-2) . None of these calls use `AttachmentRequest.content_hash`; they all use the attacker-controlled `attachment.hash()`.

Consequently, if a malicious peer returns content `C_attacker` whose hash `H_attacker != content_hash (H_real)` that was actually requested, `resolve_attachment(&H_attacker)` cannot match the batch's tracked `(attachment_index, H_real)` entry, so the legitimate, on-chain-committed instance for `H_real` is never resolved by this response. The `AttachmentsBatch` for that height is re-enqueued and retried until `max_attachment_retry_count` is exceeded, at which point it is silently dropped ("Atlas: dropping batch ... retries count exceeded") [7](#0-6) . Meanwhile, `insert_instantiated_attachment(&attachment)` stores the attacker's unrelated bytes keyed by `H_attacker` in the Atlas DB, which is a hash with no legitimate on-chain instance pointing to it (unless it happens to coincidentally collide with a genuinely requested different hash also being resolved concurrently, which would then falsely satisfy an unrelated attachment instance with wrong content).

### Impact Explanation
- A legitimate, on-chain-confirmed BNS/Atlas attachment (e.g., a name's zonefile) can be permanently prevented from being resolved by the requesting node, because the malicious source's junk response is accepted as "found" for a different, useless hash while the real request is effectively swallowed without a hash match.
- The malicious peer's reliability report is bumped as "successful" despite serving useless/wrong data, letting it accumulate an inflated reliability score and be preferred for further requests, amplifying the effect over the retry lifecycle of the node.
- This matches "High - attachment/BNS mismatch" (serving non-canonical/uncommitted data as if it satisfied a canonical, on-chain-committed hash, and denial of resolution for the correct one).

### Likelihood Explanation
- Precondition: attacker only needs to run an ordinary outbound-sync-capable peer, gossip a `GetAttachmentsInvResponse` claiming to have the attachment (setting the relevant inventory bit to 1), and respond to the follow-up `GET /v2/attachments/<hash>` with an arbitrary JSON body decodable as `GetAttachmentResponse`.
- No secrets, no privileged role, no signature checks are required — `RPCGetAttachmentRequestHandler`/`decode_atlas_get_attachment` is a plain unauthenticated RPC response path.
- Fully repeatable per attachment/batch retry cycle; attacker cost is negligible (single crafted HTTP response per request).

### Recommendation
In `decode_atlas_get_attachment` (or in `extend_with_attachments`), require the caller to pass the expected `content_hash` (available from `AttachmentRequest.content_hash`) and verify `Hash160::from_data(&attachment.content) == content_hash` before accepting the response; on mismatch, treat it as a faulty response (`report.bump_failed_requests()` and/or mark peer as faulty) rather than a success. Only insert into `context.attachments` and call `resolve_attachment`/`insert_instantiated_attachment` when this equality holds.

### Proof of Concept
Rust net test plan (in `stackslib/src/net/atlas/download.rs` or an integration test under `net::atlas`):
1. Construct an `AttachmentsBatchStateContext` with a single `AttachmentRequest{ content_hash: H_real, sources: {peer_url}, .. }`.
2. Spin up a mock HTTP peer that, on `GET /v2/attachments/<H_real>`, returns a 200 JSON body `GetAttachmentResponse{ attachment: Attachment::new(b"attacker-bytes") }` whose `hash()` is `H_attacker != H_real`.
3. Drive `BatchedRequestsState::try_proceed` through completion, then call `extend_with_attachments`.
4. Assert: `context.attachments` contains the attacker's `Attachment` (hash `H_attacker`), and `report.successful_requests` was bumped — i.e., no rejection occurred.
5. Continue through `AttachmentsDownloader::run`'s `Done` branch; assert `network.atlasdb.find_attachment(&H_real)` still returns `None`/not found, while `find_attachment(&H_attacker)` returns the attacker's junk content — proving `H_real`'s instance never transitions out of the unresolved queue, and confirming `context.attachments_batch.has_fully_succeed()` is `false`, causing eventual batch drop after `max_attachment_retry_count` retries.

### Citations

**File:** stackslib/src/net/api/getattachment.rs (L69-77)
```rust
        }

        let attachment_hash_str = captures
            .name("attachment_hash")
            .ok_or(Error::DecodeError(
                "Failed to match path to attachment_hash group".to_string(),
            ))?
            .as_str();

```

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

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

**File:** stackslib/src/net/atlas/download.rs (L188-205)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L454-474)
```rust
                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
                }

                if sources.is_empty() {
                    warn!("Atlas: could not find a peer including attachment in its inventory");
                    continue;
                }

                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
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

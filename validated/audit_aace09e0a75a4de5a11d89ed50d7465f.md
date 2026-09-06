### Title
Attacker-supplied `Attachment` content with mismatched hash is unconditionally inserted into the atlas DB without validating it against the requested `content_hash` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When `AttachmentsDownloader::run` processes the `Done` state of the `AttachmentsBatchStateMachine`, it inserts every attachment collected in `context.attachments` into `network.atlasdb` via `insert_instantiated_attachment(&attachment)` without verifying that `attachment.hash()` matches the `content_hash` originally requested in the corresponding `AttachmentRequest`. A malicious peer serving a `GET /v2/attachments/{hash}` response can therefore supply arbitrary content whose hash differs from what was requested/committed on-chain, and that unrelated content gets stored and becomes permanently retrievable.

### Finding Description
The intended invariant is that an `Attachment` retrieved for a given `content_hash` must satisfy `attachment.hash() == content_hash`, since `content_hash` originates from a confirmed on-chain `AttachmentInstance` (name operation commitment). This equality is never checked in the code path:

- `AttachmentsBatchStateContext::extend_with_attachments` (download.rs:530-558) takes the raw `StacksHttpResponse` from a peer, calls `response.decode_atlas_get_attachment()` [1](#0-0) , and unconditionally inserts `response.attachment` into `self.attachments: HashSet<Attachment>` with no comparison to `request.content_hash`.
- `decode_atlas_get_attachment` (getattachment.rs:159-165) simply JSON-parses the response body into a `GetAttachmentResponse { attachment }` struct — it performs no hash validation of the returned content against anything [2](#0-1) .
- `AttachmentsDownloader::run`, upon reaching `AttachmentsBatchStateMachine::Done`, iterates `context.attachments.drain()` and calls `network.atlasdb.find_all_attachment_instances(&attachment.hash())` followed unconditionally by `network.atlasdb.insert_instantiated_attachment(&attachment)` [3](#0-2) . Even if `find_all_attachment_instances` returns an empty vector (i.e., no on-chain-confirmed `AttachmentInstance` names this hash), the attachment is still inserted into the atlas DB.

A malicious peer that is selected as one of the `sources` for an `AttachmentRequest` (this only requires being an outbound sync peer with the requested attachment marked present in its gossiped inventory — no privileged role, secret, or admin access is needed) can respond to the GET request with a JSON body encoding any `Attachment{content: b"garbage"}`. This content's `hash()` (H2) will generally differ from the `content_hash` (H1) that was actually requested/committed. The response still passes `decode_atlas_get_attachment` (which does no hash check), gets added to `context.attachments`, and is persisted via `insert_instantiated_attachment` regardless of the "no matching instance" outcome from `find_all_attachment_instances`.

The `check_attachment_instances` function elsewhere in the same file (used for the *initial batch*/queued attachments path) does perform correct hash-keyed lookups via `find_attachment(&attachment_instance.content_hash)` before accepting data [4](#0-3) , showing that the codebase's design intent is to key attachments strictly by their content hash — but the `run()`/`extend_with_attachments` path for peer-downloaded attachments bypasses this discipline entirely.

### Impact Explanation
Any remote peer selected for an attachment fetch can inject arbitrary content into the node's persistent `attachments` SQLite table under a hash that no confirmed on-chain `AttachmentInstance` ever named. This forged data becomes permanently retrievable via `/v2/attachments/{hash}` (served straight from `find_attachment` in `getattachment.rs:104-119`), i.e., "state served that no canonical block committed" — this matches the High-severity category of "attachment/BNS mismatch." It is repeatable per distinct garbage payload (each yields a new hash) and can be used to fill the attachments table with unbounded volumes of unrelated data, misrepresenting it as validated Atlas attachment content.

### Likelihood Explanation
- Preconditions: attacker must run their own peer, be discovered as an outbound sync peer by the victim, and get selected as a `source` for at least one pending `AttachmentRequest` (achieved simply by advertising the relevant bit in its `GetAttachmentsInvResponse` inventory for a page/index the victim is missing — this is a self-reported, ungated signal). No RPC secret, peer key, or StackerDB slot is required.
- Attacker cost: trivial — craft one HTTP 200 JSON response body per request.
- Remote reachability: yes, via the node's normal outbound attachment-sync HTTP client connecting to the attacker-controlled data URL.
- Repeatability: unlimited, one forged attachment per malicious response, and the flow (queued via `AttachmentsBatch`/priority queue) recurs continuously as the node keeps syncing.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or in `AttachmentsDownloader::run` before calling `insert_instantiated_attachment`), verify `response.attachment.hash() == request.content_hash` before accepting the attachment into `self.attachments` / before inserting into `atlasdb`. Discard and treat as a failed/faulty response (bump `report.bump_failed_requests()`, and consider marking/deregistering the offending peer) if the hash does not match.

### Proof of Concept
Rust test plan in `stackslib::net::atlas::download` (or a new test module):
1. Construct an `AttachmentsBatchStateContext` with a `peers` map containing one `UrlString` and empty `ReliabilityReport`.
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains one entry: key = `AttachmentRequest{content_hash: H1, sources: {url: report}, stacks_block_height: 1, canonical_stacks_tip_height: None}`, value = `Some(StacksHttpResponse)` constructed to decode via `decode_atlas_get_attachment` into `GetAttachmentResponse{attachment: Attachment{content: b"garbage".to_vec()}}` (whose real hash is H2 ≠ H1). This can be built directly by constructing the `StacksHttpResponse` with a JSON body matching `GetAttachmentResponse`'s serde format, bypassing the network layer.
3. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments.contains(&Attachment{content: b"garbage".to_vec()})` — confirming no hash-check filtering occurred at this layer.
4. Alternatively, drive it end-to-end through `AttachmentsDownloader::run`'s `Done` branch (constructing the `Done(context)` state directly) with an `AtlasDB` test instance that has zero `AttachmentInstance` rows referencing H2, then assert `atlas_db.find_attachment(&H2).unwrap().is_some()` returns `true` — demonstrating persisted, retrievable, non-canonical attachment data with no corresponding on-chain commitment.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L153-162)
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
```

**File:** stackslib/src/net/atlas/download.rs (L246-254)
```rust
            } else if let Ok(Some(entry)) =
                atlas_db.find_attachment(&attachment_instance.content_hash)
            {
                // Do we already have a matching validated attachment
                do_if_found(atlas_db, &attachment_instance)?;
                debug!(
                    "Atlas: inserting and pairing new attachment instance to existing attachment"
                );
                resolved_attachments.push((attachment_instance, entry));
```

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
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

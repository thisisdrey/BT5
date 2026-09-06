### Title
Malicious sync peer poisons AtlasDB with arbitrary content under a self-consistent but on-chain-uncommitted hash - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsDownloader::run` in `stackslib/src/net/atlas/download.rs` unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for every `Attachment` returned by a queried peer, without ever checking that the returned content's hash matches the `content_hash` that was actually being requested. A malicious peer selected as an outbound sync peer can therefore respond to any `AttachmentRequest` with arbitrary content, causing the node to store a `was_instantiated = 1` row in the `attachments` table under `Hash160(attacker_content)`, a hash that no `Checked` `AttachmentInstance` (i.e., no on-chain commitment) ever referenced. `RPCGetAttachmentRequestHandler::try_handle_request` (`stackslib/src/net/api/getattachment.rs:104-118`) will then serve that content to any unprivileged RPC client under that hash.

### Finding Description
The claimed equality is: `served-content-hash == on-chain-committed-content-hash` for every hash the `/v2/attachments/{hash}` endpoint returns 200 for.

- `RPCGetAttachmentRequestHandler::try_handle_request` [1](#0-0)  calls `AtlasDB::find_attachment`, which is a raw lookup: `SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 1` [2](#0-1) . This query never joins against `attachment_instances` or checks `AttachmentInstanceStatus::Checked`; it trusts the `attachments` table's `was_instantiated` flag alone.
- The `attachments` table is populated with `was_instantiated=1` rows from `insert_instantiated_attachment`, which is called from two places: (1) `check_attachment_instances`, gated behind matching a `Checked`/`Queued` `AttachmentInstance` [3](#0-2) , and (2) `AttachmentsDownloader::run`, in the `Done` branch of the batch state machine, which is NOT gated at all: [4](#0-3) . Here, for every `attachment` drained from `context.attachments` (populated by `extend_with_attachments`), the code computes `attachment.hash()` from the actual bytes returned by the peer, looks up `find_all_attachment_instances(&attachment.hash())`, and **regardless of whether that lookup returns anything**, calls `insert_instantiated_attachment(&attachment)`, writing a `was_instantiated=1` row keyed by `attachment.hash()`.
- `extend_with_attachments` [5](#0-4)  takes the peer's HTTP response, decodes it via `decode_atlas_get_attachment()`, and inserts `response.attachment` into `self.attachments` if decoding succeeds — there is no check that `response.attachment.hash()` equals the `content_hash` field of the `AttachmentRequest` that was sent (`stackslib/src/net/atlas/download.rs` `AttachmentRequest::make_request_type`, which builds `GET /v2/attachments/{self.content_hash}`).
- `Attachment::hash()` is simply `Hash160::from_data(&self.content)` [6](#0-5)  — i.e., it is fully attacker-controlled since content is attacker-controlled.

**Exploit flow:**
1. Attacker's node becomes one of the victim's outbound sync peers (`network.get_outbound_sync_peers()`), which is within the granted attacker capability of "run their own peer."
2. The victim's `AttachmentsDownloader` sends the attacker a legitimate `GET /v2/attachments/{X}` request for some content-hash `X` that corresponds to a real, on-chain `Checked` `AttachmentInstance` the victim is trying to resolve.
3. The attacker responds with a 200 JSON body encoding arbitrary bytes `Y` (any content of the attacker's choosing), unrelated to `X`.
4. `decode_atlas_get_attachment()` succeeds (it only hex-decodes the string; no hash check against `X`), so `Y` is inserted into `context.attachments`.
5. In `run()`, `attachment.hash()` computes `H = Hash160(Y)` (not `X`). `find_all_attachment_instances(H)` returns empty (no instance ever committed to `H`), but `insert_instantiated_attachment(&attachment)` executes anyway, writing `(hash=H, content=Y, was_instantiated=1)` into the `attachments` table.
6. Any remote unprivileged client can now `GET /v2/attachments/{H}` and receive `Y` with a 200 response, even though no BNS/Atlas on-chain event ever committed to hash `H`.

Existing guards (`MAX_MESSAGE_LEN`, RPC auth, tip resolution) do not apply here: this is a semantic content-integrity check missing entirely from the downloader pipeline, not a wire-format or auth issue.

### Impact Explanation
This directly matches the "High" category "attachment/BNS mismatch": the node serves arbitrary attacker-chosen data as though it were canonical Atlas/BNS attachment content, when in fact no on-chain name/attachment operation ever committed to that hash. Any client (wallets, indexers, BNS resolvers) that trusts `/v2/attachments/{hash}` responses as canonical (relying on the invariant that a 200 response implies an on-chain commitment) can be fed forged data. The forged row persists in the victim's `attachments` table until evicted, and is served to every unprivileged remote client that queries that hash — fully repeatable and requires only one malicious response per targeted hash.

### Likelihood Explanation
- Precondition: the attacker's peer must be selected among the victim's `get_outbound_sync_peers()` and respond to an `AttachmentRequest` for at least one legitimate pending attachment instance. This requires no privileged role, RPC secret, or admin access — only running a reachable P2P peer, which is within the stated attacker capability ("run their own peer, ... gossip messages").
- Attacker cost is a single malicious HTTP response per exploited hash; fully repeatable for every attachment request routed to the attacker.
- No additional network condition beyond being an eligible sync peer is required.

### Recommendation
In `stackslib/src/net/atlas/download.rs`, verify the downloaded content's hash against the requested `content_hash` before accepting it: either check `response.attachment.hash() == request.content_hash` in `extend_with_attachments` (dropping/penalizing the peer and bumping `report.bump_failed_requests()` on mismatch), or in `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `find_all_attachment_instances(&attachment.hash())` is non-empty (i.e., only persist content that actually resolves a `Checked` instance), discarding attachments whose hash matches no pending instance.

### Proof of Concept
Rust test plan (extending `stackslib/src/net/atlas/tests.rs` or `stackslib/src/net/api/tests/getattachment.rs` style):
1. Construct an `AtlasDB` (in-memory) with a `Checked` `AttachmentInstance` for `content_hash = X` (via `insert_attachment_instance`/`queue_attachment_instance` + `mark_attachment_instance_checked`), but no attachment content yet.
2. Simulate the `AttachmentsBatchStateMachine::Done` completion path directly (or via `AttachmentsDownloader::run`'s `Done` branch logic) by constructing `context.attachments` containing an `Attachment` with content `Y` such that `Hash160(Y) = H != X` (attacker-supplied, mismatched content).
3. Call the equivalent of the `Done` handling code: compute `find_all_attachment_instances(&Attachment(Y).hash())` (expect empty vec), then call `atlasdb.insert_instantiated_attachment(&Attachment(Y))`.
4. Assert: `atlasdb.find_attachment(&H).unwrap()` returns `Some(Attachment(Y))` even though `atlasdb.find_all_attachment_instances(&H).unwrap()` is empty — proving a `was_instantiated=1` row exists for a hash with no `Checked` instance.
5. Feed this AtlasDB into an `RPCGetAttachmentRequestHandler` test (as in `stackslib/src/net/api/tests/getattachment.rs::test_try_make_response`), issue `StacksHttpRequest::new_getattachment(addr, H)`, and assert the response is `200 OK` with `resp.attachment.content == Y` — demonstrating the endpoint serves content for a hash lacking any `Checked` `AttachmentInstance`, which should instead 404.

### Citations

**File:** stackslib/src/net/api/getattachment.rs (L104-118)
```rust
        let attachment_res = node.with_node_state(
            |network, _sortdb, _chainstate, _mempool, _rpc_args| match network
                .get_atlasdb()
                .find_attachment(&attachment_hash)
            {
                Ok(Some(attachment)) => Ok(GetAttachmentResponse { attachment }),
                _ => {
                    let msg = "Unable to find attachment".to_string();
                    warn!("{msg}");
                    Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new(msg),
                    ))
                }
            },
```

**File:** stackslib/src/net/atlas/db.rs (L641-648)
```rust
    pub fn find_attachment(&self, content_hash: &Hash160) -> Result<Option<Attachment>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 1"
            .to_string();
        let args = params![hex_content_hash];
        let row = query_row::<Attachment, _>(&self.conn, &qry, args)?;
        Ok(row)
    }
```

**File:** stackslib/src/net/atlas/download.rs (L152-169)
```rust
        match progress {
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

**File:** stackslib/src/net/atlas/download.rs (L255-262)
```rust
            } else if let Ok(Some(attachment)) =
                atlas_db.find_uninstantiated_attachment(&attachment_instance.content_hash)
            {
                // Do we already have a matching inboxed attachment
                atlas_db.insert_instantiated_attachment(&attachment)?;
                do_if_found(atlas_db, &attachment_instance)?;
                debug!("Atlas: inserting and pairing new attachment instance to inboxed attachment, now validated");
                resolved_attachments.push((attachment_instance, attachment));
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

**File:** stackslib/src/net/atlas/mod.rs (L158-160)
```rust
    pub fn hash(&self) -> Hash160 {
        Hash160::from_data(&self.content)
    }
```

### Title
Unvalidated attacker-controlled data from Atlas peer responses is persisted and served as canonical BNS attachment content - (File: `stackslib/src/net/atlas/download.rs`, `stackslib/src/net/api/getattachment.rs`)

### Summary
`AttachmentsDownloader::run` in `stackslib/src/net/atlas/download.rs` calls `AtlasDB::insert_instantiated_attachment(&attachment)` unconditionally for every `Attachment` object returned in an `AttachmentRequest` response, without ever checking that `attachment.hash()` matches a `content_hash` referenced by any known, on-chain-committed `AttachmentInstance`. Since `insert_instantiated_attachment` stores rows keyed by the locally-computed `attachment.hash()` (not by the hash that was originally requested), any peer that a victim node treats as an "outbound sync peer" can return arbitrary bytes for an attachment request and have them permanently persisted as `was_instantiated = 1`, after which `RPCGetAttachmentRequestHandler::try_handle_request` in `getattachment.rs` will serve that content to any unauthenticated remote caller of `GET /v2/attachments/{hash}`.

### Finding Description
The equality that should hold is: for every row in the `attachments` table with `was_instantiated = 1` (i.e., servable via `find_attachment`), its `hash` must correspond to a `content_hash` that a confirmed on-chain BNS name operation actually committed to via a validated `AttachmentInstance`.

This is violated in `AttachmentsDownloader::run`: [1](#0-0) 

When the `AttachmentsBatchStateMachine` reaches `Done`, the code iterates `context.attachments` (populated purely from HTTP responses in `extend_with_attachments`) and, for each one, unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` — it does this *before* even checking whether `find_all_attachment_instances(&attachment.hash())` returned anything. If it returns an empty vector (i.e., no `AttachmentInstance` in the DB actually references this hash), the attachment is still written to the `attachments` table with `was_instantiated = 1`. [2](#0-1) 

`extend_with_attachments` inserts `response.attachment` into a `HashSet<Attachment>` with no verification that the returned attachment's hash matches the `content_hash` that was actually requested (`AttachmentRequest::content_hash`). Because a peer is free to reply to any `GetAttachment` request with arbitrary content, and `insert_instantiated_attachment` keys the row on `attachment.hash()` (the hash computed from the returned bytes) rather than on the originally-requested hash: [3](#0-2) 

an attacker-controlled peer can serve content `C` whose hash `hash(C) = Y` has never been referenced by any confirmed name operation, and it will be written into the `attachments` table as validated (`was_instantiated = 1`).

`RPCGetAttachmentRequestHandler::try_handle_request` then serves this content to any unauthenticated remote caller with no additional commitment check: [4](#0-3) 

and `find_attachment` simply looks up by hash and `was_instantiated`: [5](#0-4) 

with no join against `attachment_instances` to confirm on-chain backing.

### Impact Explanation
An attacker that becomes an outbound sync peer of a victim node (a permissionless role in the Stacks P2P network — no RPC secret, admin role, or privileged key needed) can cause the victim to persist arbitrary attacker-chosen bytes under a hash `Y` for which no BNS name operation ever committed. Any unauthenticated remote party can then retrieve this content via `GET /v2/attachments/{Y}`, receiving data the node presents as validated/canonical Atlas content despite there being no on-chain commitment to it. This matches the "High — serving non-canonical state as canonical / attachment-BNS mismatch" category, and repeated requests for different fabricated payloads accumulate indefinitely in the victim's `attachments` table, since there is no eviction path for `was_instantiated = 1` rows (eviction only targets uninstantiated/unresolved rows) — contributing to unbounded disk growth.

### Likelihood Explanation
The attacker only needs to run a normal Stacks peer that a victim selects as an outbound sync peer (`network.get_outbound_sync_peers()`), which requires no special privilege, secret, or trust relationship — it is the ordinary permissionless P2P gossip role explicitly allowed in scope. The attacker must respond to a `GetAttachment` request the victim issues (triggered when the victim has an `AttachmentInstance` referencing some real hash it wants); the attacker's malicious reply for that hash is what gets stored (keyed by its actual computed hash, not the requested one), so the attacker can plant content under a hash of their choosing by first computing `hash(C)` for their chosen payload `C` and replying with it whenever asked for any attachment. This is repeatable for every batch cycle and low-cost.

### Recommendation
In `AttachmentsBatchStateMachine`'s handling of downloaded attachments (`extend_with_attachments` in `download.rs`), verify that `attachment.hash()` equals the `AttachmentRequest.content_hash` that was actually requested before adding it to `context.attachments`. Additionally, in `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `find_all_attachment_instances(&attachment.hash())` returns at least one matching, checked `AttachmentInstance` (i.e., there is a real on-chain-derived commitment to that hash); otherwise discard the response and penalize the reporting peer's reliability score.

### Proof of Concept
1. In a `stackslib::net::atlas::download` or `net::api::tests::getattachment` test, construct a `PeerNetwork`/`AtlasDB` with no `AttachmentInstance` referencing hash `Y`.
2. Simulate an `AttachmentsBatchStateMachine::Done` state where `context.attachments` contains an `Attachment` whose content hashes to `Y` (attacker-supplied bytes), by directly invoking the `Done` arm logic (or by mocking the HTTP response from a peer to a `GetAttachment` request with arbitrary body).
3. Call `AttachmentsDownloader::run`, then query `AtlasDB::find_attachment(&Y)` and confirm it returns `Some(attachment)` with `was_instantiated = 1`, despite `find_all_attachment_instances(&Y)` returning `[]` beforehand.
4. Issue `RPCGetAttachmentRequestHandler` handling of `StacksHttpRequest::new_getattachment(host, Y)` and assert the response is HTTP 200 with the attacker's content, confirming `getattachment.rs:104-119` serves data for a hash with no confirmed on-chain commitment.

### Citations

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

**File:** stackslib/src/net/api/getattachment.rs (L104-119)
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
        );
```

### Title
Unvalidated attachment content is stored as "instantiated" in `AtlasDB` without verifying it matches any on-chain `AttachmentInstance.content_hash`, letting a malicious sync peer poison `GET /v2/attachments/<hash>` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsDownloader::run` unconditionally calls `AtlasDB::insert_instantiated_attachment` for every `Attachment` returned by a peer during attachment-sync, regardless of whether `find_all_attachment_instances(&attachment.hash())` found any matching on-chain commitment. Combined with `extend_with_attachments` not verifying that a peer's `GetAttachmentResponse.attachment` actually corresponds to the `content_hash` that was requested, a malicious outbound sync peer can inject arbitrary content into a victim's `AtlasDB` under any hash, which is then served verbatim by `RPCGetAttachmentRequestHandler::try_handle_request` via `find_attachment`.

### Finding Description
The claimed equality — "attachment_hash served by `find_attachment` == hash committed by a confirmed name op (`AttachmentInstance.content_hash`)" — is broken:

1. When the `AttachmentsDownloader` batch state machine finishes downloading (`AttachmentsBatchStateMachine::Done`), `extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-559) decodes each peer's HTTP response via `decode_atlas_get_attachment()` and inserts the resulting `Attachment` into `self.attachments` (a `HashSet`) with **no check that `response.attachment.hash()` equals the `content_hash` that was actually requested** for that peer/request pair. [1](#0-0) 

2. `AttachmentsDownloader::run` then iterates every attachment in that set and unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)`, storing it as `was_instantiated = 1` — the check against `find_all_attachment_instances` only affects which cached instances get *paired* with the data for gossip purposes, it does not gate the `insert_instantiated_attachment` call itself: [2](#0-1) 

3. `AtlasDB::insert_instantiated_attachment` performs no validation against any `AttachmentInstance.content_hash`; it simply writes `(attachment.hash(), content, was_instantiated=1)` into the `attachments` table: [3](#0-2) 

4. `AtlasDB::find_attachment` returns any row from `attachments` with `was_instantiated = 1` matching the queried hash, with no join/verification against `attachment_instances`: [4](#0-3) 

5. `RPCGetAttachmentRequestHandler::try_handle_request` serves whatever `find_attachment` returns directly to any unauthenticated RPC caller: [5](#0-4) 

Attacker's exact message/flow: the attacker is selected as one of the node's `get_outbound_sync_peers()` for attachment sync (a normal, unprivileged P2P/HTTP peer relationship — no secret or admin role required). When the victim's `AttachmentsDownloader` issues a `GET /v2/attachments/<hash>`-style request to the attacker's advertised data URL for some queued `content_hash`, the attacker's HTTP server replies with a `GetAttachmentResponse` containing arbitrary bytes whose SHA-derived `Hash160` may not equal the requested hash (or may equal an uncommitted hash chosen by the attacker). Because `extend_with_attachments` never checks `attachment.hash() == requested content_hash`, and `insert_instantiated_attachment` is invoked unconditionally on `Done`, this attacker-chosen content gets persisted under `attacker_hash = Attachment::hash(content)` in `AtlasDB.attachments` with `was_instantiated = 1`, without any corresponding row in `attachment_instances` referencing that hash. Any later unauthenticated client hitting `GET /v2/attachments/<attacker_hash>` on that victim node receives `GetAttachmentResponse{ attachment: attacker_content }`, i.e., data that was never the subject of a confirmed BNS/name-registration attachment commitment.

### Impact Explanation
A remote unprivileged peer that participates in normal Atlas attachment sync can plant arbitrary content in a victim Stacks node's local `AtlasDB` and have it served as if it were legitimately committed BNS zonefile/attachment data via the public `/v2/attachments/<hash>` RPC endpoint. This is a High-severity "attachment/BNS mismatch": the node serves non-canonical, attacker-controlled data to any RPC client trusting that endpoint, without any confirmed on-chain name operation backing it. The attack is repeatable per attachment slot and affects every unauthenticated caller of that victim node's RPC endpoint.

### Likelihood Explanation
Preconditions: the attacker must be one of the victim's outbound sync peers with a reachable HTTP data URL (`network.get_outbound_sync_peers()` / `get_data_url`) so it gets included in an `AttachmentsBatch` request — this is achievable by any node that peers normally with the victim, no special privilege, secret, or role required. Attack cost is a single crafted HTTP response per targeted content hash, fully remote, and repeatable for as many hashes as the attacker can get queued/requested.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs), verify that `response.attachment.hash()` matches the specific `content_hash` that was requested for that `AttachmentRequest` before inserting it into `self.attachments`; discard and penalize (bump_failed_requests) any response whose hash does not match. Additionally, in `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `find_all_attachment_instances(&attachment.hash())` (or an uninstantiated-attachment record) is non-empty, so content with no corresponding `AttachmentInstance` is never persisted as validated/instantiated.

### Proof of Concept
Rust test plan (in `stackslib/src/net/atlas/tests.rs` style or `stackslib/src/net/api/tests/getattachment.rs`):
1. Construct an `AtlasDB` and an `AttachmentsBatchStateContext`/`AttachmentsDownloader` as in existing tests (see `stackslib/src/net/atlas/tests.rs`).
2. Simulate a `BatchedRequestsResult<AttachmentRequest>` where `succeeded` contains a request for `content_hash = H1` but the mocked HTTP response's `GetAttachmentResponse.attachment` has content whose real hash is `H2 != H1` (or simply craft content for a hash `H2` that was never used in any `new_attachment_instance_from` call, i.e., no `AttachmentInstance` exists for it).
3. Call `context.extend_with_attachments(&mut results)` and then drive `AttachmentsDownloader::run` to `Done`, confirming `atlas_db.insert_instantiated_attachment` gets invoked for `H2`.
4. Assert `atlas_db.find_all_attachment_instances(&H2).unwrap().is_empty()` (no on-chain instance references `H2`).
5. Issue the RPC handler `RPCGetAttachmentRequestHandler::try_handle_request` for `attachment_hash = H2` and assert the returned `GetAttachmentResponse.attachment.hash() == H2`, proving the node served attacker content for a hash with zero backing `AttachmentInstance`/name-op commitment.

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

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
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

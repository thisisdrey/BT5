## Title
Attachment content is stored and cross-linked by peer-supplied data hash without binding it to the originally-requested `content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
The Atlas attachment downloader requests a specific attachment by `content_hash` from a remote peer (`AttachmentRequest.content_hash`, `AttachmentRequest::make_request_type`), but when the peer's response is received, the code never checks that the returned payload's hash actually equals the `content_hash` that was requested before accepting it into `self.attachments`. [1](#0-0) [2](#0-1) 

### Finding Description
`extend_with_attachments` iterates over `results.succeeded` (keyed by `AttachmentRequest`, which carries the expected `content_hash`) and, for each successful HTTP response, decodes it and does:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
``` [3](#0-2) 
There is no comparison between `request.content_hash` (the key that was asked for) and `Hash160::from_data(&response.attachment.content)` (the actual content received). The `AttachmentRequest` struct that carried the expected hash is dropped (`results.succeeded.drain()` discards the key/request pairing beyond using it to look up the reliability report), so the association between "what we asked for" and "what we got" is lost entirely at this stage. [4](#0-3) 

Later, when the batch is `Done`, each accumulated attachment is paired to attachment instances purely by re-hashing the *received* bytes:
```
let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash());
network.atlasdb.insert_instantiated_attachment(&attachment)?;
``` [5](#0-4) 
This re-derivation of the hash from the actual bytes does provide self-consistency for *that* particular hash-to-content binding (the DB always associates content with `Hash160::from_data(content)`), but it means the downloader accepted whatever bytes an arbitrary queried peer chose to send back for a `GET /v2/attachments/<hash>` request without ever verifying that the peer answered the specific hash that was asked. A malicious/faulty peer can respond to a request for `content_hash = X` with unrelated data `Y` (`hash(Y) != X`). The download logic has no failure path for this: it simply accepts `Y`, computes `hash(Y)`, and stores it under `hash(Y)` in `AtlasDB` via `insert_instantiated_attachment`, then, if any `AttachmentInstance` on-chain happens to reference `hash(Y)`, serves it as that instance's resolved data via `GET /v2/attachments/{content_hash}` (`RPCGetAttachmentRequestHandler::try_handle_request`, `find_attachment`). [6](#0-5) [7](#0-6) 

Critically, the batch bookkeeping (`AttachmentsBatch::resolve_attachment`) marks the *originally requested* hash as resolved based on the hash of whatever was received, not on whether the specific requested hash was fulfilled — meaning the node can believe it satisfied the request for `X` while it never actually fetched `X`'s content, silently leaving the true attachment for `X` permanently unresolved (or masked) while wasting the retry budget, and/or ingesting attacker-chosen content into the node's Atlas store under a hash that happens to collide with a hash referenced by some other legitimate `AttachmentInstance`.

### Impact Explanation
This is a High-severity issue matching "serving non-canonical/mismatched data as canonical" (attachment/BNS mismatch is explicitly called out in the scope's impact list). Any of the outbound peers a node syncs Atlas data from (unauthenticated, remote, no special role needed) can supply attachment content that was never actually verified against the hash the requesting node asked for. Because attachments back BNS name-related metadata resolution via the Atlas subsystem, an attacker-controlled or compromised peer can influence what content ends up bound to a `content_hash` referenced by other legitimate `AttachmentInstance`s, and clients calling the node's `/v2/attachments/{content_hash}` RPC will receive whatever was stored — with no cryptographic assurance the content was ever checked against a legitimate source at fetch time.

### Likelihood Explanation
The attacker only needs to run/serve as one of the outbound peers a victim node fetches attachments from (`network.get_outbound_sync_peers()` / any node offering `/v2/attachments/<hash>`), and answer such a GET request with arbitrary bytes. No signature, authentication or special network position beyond serving normal Atlas HTTP traffic is required, making this trivially reachable by any peer participating in Atlas gossip.

### Recommendation
In `extend_with_attachments` (or immediately upon decoding the HTTP response), compute `Hash160::from_data(&response.attachment.content)` and compare it to `request.content_hash` before inserting into `self.attachments`; reject/penalize (bump_failed_requests) any response whose content hash does not match the requested hash, mirroring the pattern already used for signature verification in StackerDB (`SlotMetadata::verify` checking key against expected signer) so that content-addressed data is validated against the identifier used to fetch it, not merely re-derived after acceptance.

### Proof of Concept
1. Node A queues an `AttachmentRequest { content_hash: X, sources: {peer_B_url}, .. }` because some `AttachmentInstance` references content hash `X`.
2. Peer B (attacker-controlled) receives `GET /v2/attachments/X` and returns `GetAttachmentResponse { attachment: Attachment { content: Y } }` where `Hash160::from_data(Y) != X`.
3. `extend_with_attachments` decodes the response and calls `self.attachments.insert(response.attachment)` with no check against `X`. [3](#0-2) 
4. When the batch completes, `attachment.hash()` (i.e., `Hash160::from_data(Y)`) is used to find and resolve *whatever* `AttachmentInstance`s reference that hash, and `insert_instantiated_attachment` stores `Y` in the AtlasDB. [5](#0-4) 
5. The batch entry for the original request `X` is marked resolved/completed even though the true content for `X` was never fetched, and any client that later queries `GET /v2/attachments/{hash(Y)}` on this node receives `Y` as if it were validated Atlas content. [8](#0-7)

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

**File:** stackslib/src/net/atlas/download.rs (L1065-1071)
```rust
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct AttachmentRequest {
    pub content_hash: Hash160,
    pub sources: HashMap<UrlString, ReliabilityReport>,
    pub stacks_block_height: u64,
    pub canonical_stacks_tip_height: Option<u64>,
}
```

**File:** stackslib/src/net/api/getattachment.rs (L93-120)
```rust
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
        let attachment_hash = self
            .attachment_hash
            .take()
            .ok_or(NetError::SendError("Missing `attachment_hash`".into()))?;

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
        let attachment = match attachment_res {
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

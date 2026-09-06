### Title
Attachment content returned by a peer is never verified against the requested content hash before being accepted into AtlasDB - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GetAttachmentResponse` via `decode_atlas_get_attachment` and inserts `response.attachment` into `self.attachments` without ever checking that `attachment.hash() == request.content_hash`. A malicious peer selected as the download source can therefore supply arbitrary attachment bytes for any requested content hash, which get stored in AtlasDB as "instantiated" while the actually-committed attachment remains permanently unresolved.

### Finding Description
The claimed equality `attachment.hash() == request.content_hash` is never checked anywhere in the code path from HTTP response to AtlasDB insertion:

- `RPCGetAttachmentRequestHandler::try_handle_request` (stackslib/src/net/api/getattachment.rs:93-130) on the *server* side looks up by the requested hash from its own DB, so a peer serving its own honest DB naturally returns matching content — but an attacker running their own peer/data-server controls what content they return for any path, since `try_parse_request` only validates the hash's hex format (line 78-81) and the server-side handler code is irrelevant once the attacker is the one responding.
- On the *client/requester* side, `StacksHttpResponse::decode_atlas_get_attachment` (stackslib/src/net/api/getattachment.rs:159-165) just JSON-deserializes the body into `GetAttachmentResponse { attachment }` — no hash check.
- `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558) then does:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
```
`request.content_hash` (the value that was actually requested, from `AttachmentRequest`) is never read or compared to `response.attachment.hash()` here.
- Downstream, `AttachmentsDownloader::run` (download.rs:153-169) does:
```
for attachment in context.attachments.drain() {
    let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash())?;
    network.atlasdb.insert_instantiated_attachment(&attachment)?;
    ...
    context.attachments_batch.resolve_attachment(&attachment.hash())
}
```
It looks up pending `AttachmentInstance`s and marks the batch resolved using `attachment.hash()` — i.e., the hash of whatever bytes the attacker sent — not the originally requested `content_hash`. Since the attacker's forged content hashes to a different value, `find_all_attachment_instances` finds nothing (so the real pending instance for the true content hash is never resolved), yet `insert_instantiated_attachment` still writes the forged attachment into AtlasDB as instantiated (`was_instantiated=1`), and `resolve_attachment` marks the batch as done for a hash nobody actually requested.

The attacker's peer is legitimately selected via `AttachmentRequest::get_most_reliable_source`, since any outbound sync peer's advertised data URL is eligible — no authentication/secret is required to serve HTTP GET `/v2/attachments/<hash>` responses.

### Impact Explanation
A remote, unprivileged peer that is selected as a download source can:
1. Poison the local node's AtlasDB with attacker-chosen bytes stored as a legitimately "instantiated" attachment (growth of garbage data in the `attachments` table).
2. Permanently prevent resolution of the real, on-chain-committed attachment for that content hash — the `AttachmentInstance` queue entry for the true hash is never matched/resolved because the lookup key (`attachment.hash()`) differs from the real `content_hash`. The batch's `resolve_attachment` call also uses the wrong hash, so the true entry is retried until `max_attachment_retry_count` and then dropped (download.rs:187-205), permanently marking a valid, committed attachment as missing.

This matches the "High: attachment/BNS mismatch" impact category — the node can be made to treat a canonical, on-chain-committed name/attachment as unresolved/missing due to unauthenticated forged gossip data, without ever detecting the mismatch.

### Likelihood Explanation
- Precondition: the node must have added the attacker's peer as an outbound sync peer with a Atlas-enabled `data_url` (a normal state reachable by any peer that advertises itself over the P2P protocol; no privileged role, secret, or admin access required).
- The attacker only needs to respond to a normal RPC-style HTTP GET on `/v2/attachments/<hash>` that gets routed to them because `get_most_reliable_source` picked them (e.g., by having a good "reliability report", or simply being one of few candidates).
- Cost is a single crafted HTTP response; the attack is trivially repeatable per attachment/content_hash of interest.
- No cryptographic material, keys, or secrets are needed — this is a bare content-integrity check that is missing.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558), after decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; treat a mismatch as a failed request (`report.bump_failed_requests()`) and optionally penalize/deregister the offending peer.

### Proof of Concept
Rust test plan (stackslib/src/net/atlas/download.rs or a new test module):
1. Construct an `AttachmentsBatchStateContext` with one peer and a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains an `AttachmentRequest { content_hash: Hash160::from_data(b"right"), .. }` mapped to `Some(StacksHttpResponse)` that decodes (via a stub/mock of `decode_atlas_get_attachment`) to `GetAttachmentResponse { attachment: Attachment { content: b"wrong".to_vec() } }`.
2. Call `extend_with_attachments`, then feed the resulting context through the `Done` handling logic in `AttachmentsDownloader::run` against a test `AtlasDB` that has a pending `AttachmentInstance` for `content_hash = Hash160::from_data(b"right")`.
3. Assert:
   - `atlasdb.find_attachment(&Hash160::from_data(b"right"))` returns `Ok(None)` (real attachment still unresolved).
   - `atlasdb.find_attachment(&Hash160::from_data(b"wrong"))` returns `Ok(Some(Attachment{content: b"wrong"}))` with `was_instantiated = 1` (forged content stored as instantiated).
   - The `AttachmentInstance` for `content_hash = right` remains unresolved/still queued (not resolved by `resolve_attachment`), confirming the queued instance was never matched to the real attachment.
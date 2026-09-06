Confirmed: at `extend_with_attachments` (download.rs:530-558), when a peer's response to an `AttachmentRequest` is decoded via `decode_atlas_get_attachment()`, the resulting `attachment.attachment` is inserted into `self.attachments` (a `HashSet<Attachment>`) with **no check that its actual content hash matches the `request.content_hash` that was requested**. This confirms the vulnerability described in the question.

### Title
Unvalidated attachment content is persisted to `AtlasDB` regardless of requested/on-chain hash - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsDownloader::extend_with_attachments` stores whatever `Attachment` bytes a peer returns for an `AttachmentRequest` without verifying that `Hash160::from(attachment.content)` equals the `content_hash` that was actually requested. `run()` then unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for every drained attachment, even when `find_all_attachment_instances(&attachment.hash())` returns an empty `Vec`, meaning no confirmed on-chain `AttachmentInstance` references that hash.

### Finding Description
The broken equality is: stored rows in the `attachments` table should equal only those hashes referenced by a `Checked` `AttachmentInstance` derived from a confirmed BNS operation. In `AttachmentsBatchStateContext::extend_with_attachments` (download.rs:530-558):
```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
```
there is no comparison between the decoded attachment's real hash and `request.content_hash`. Then in `AttachmentsDownloader::run` (download.rs:152-169):
```rust
for attachment in context.attachments.drain() {
    let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash())...;
    network.atlasdb.insert_instantiated_attachment(&attachment)...;
    ...
}
```
`insert_instantiated_attachment` is called unconditionally regardless of whether `attachments_instances` is empty. A malicious peer that is in `network.get_outbound_sync_peers()` (any outbound-connected node, which is remotely reachable with no privileged role) can answer a legitimate `AttachmentRequest` (for a real, requested `content_hash`) with an arbitrary garbage payload of its own choosing (up to `attachments_max_size`). Because the hash of the returned payload is never checked against the requested hash before storage, the garbage attachment is written into `AtlasDB` and persists on disk permanently, with no `AttachmentInstance` ever referencing it.

### Impact Explanation
Any peer this node syncs with can cause the node to write attacker-chosen, unbounded-content-hash-mismatched blobs into its local `attachments` table on disk, one write per crafted response, up to `attachments_max_size` bytes per write. This is unauthenticated write of unvalidated data into persistent node state driven entirely by remote responses to normal Atlas attachment sync traffic, matching the "unauthenticated/unauthorized write to state" Critical category, and enabling storage-growth amplification: N attachment requests → N permanent disk writes regardless of content correctness.

### Likelihood Explanation
Preconditions are minimal and attacker-controlled: the attacker only needs to be a reachable outbound sync peer for the victim's Atlas downloader (i.e., run their own node and be selected via `get_outbound_sync_peers()`/included in `AttachmentsBatchStateContext.peers`), a routine, low-cost, and repeatable condition for any P2P participant. The attacker does not need the node's RPC secret nor any privileged role — they simply respond to attachment inventory/content requests the victim node initiates as part of normal Atlas sync. This is fully repeatable: each additional distinct requested `content_hash` responded to with garbage yields another permanent stored row.

### Recommendation
In `extend_with_attachments` (or immediately before calling `insert_instantiated_attachment` in `run`), verify that the decoded attachment's hash (`Hash160::from(attachment.content)` / `attachment.hash()`) equals the `request.content_hash` that was requested before inserting into `self.attachments`; discard and penalize (`bump_failed_requests`) the peer on mismatch. Additionally, in `run()`, only call `insert_instantiated_attachment` when `find_all_attachment_instances` returns a non-empty result (i.e., a confirmed `AttachmentInstance` actually references that hash), otherwise route the payload to `insert_uninstantiated_attachment`/discard rather than the validated table.

### Proof of Concept
Rust test in `stackslib::net::atlas::tests`:
1. Construct an `AttachmentsBatchStateContext` with a `BatchedRequestsResult<AttachmentRequest>` where `succeeded` contains N distinct `AttachmentRequest`s, each with a distinct legitimate `content_hash` (as would come from a real `AttachmentInstance`/BNS op), paired with N `StacksHttpResponse`s whose `decode_atlas_get_attachment()` yields `Attachment { content: garbage_bytes }` such that `Hash160::from(garbage_bytes) != content_hash` for all N.
2. Call `context.extend_with_attachments(&mut results)` then drive `AttachmentsDownloader::run` to the `Done` branch (or directly exercise the drain loop) against a fresh `AtlasDB` with zero `AttachmentInstance` rows referencing any of the garbage hashes.
3. Assert, for each of the N garbage hashes, `atlasdb.find_attachment(&garbage_hash)` returns `Ok(Some(_))` and `atlasdb.find_all_attachment_instances(&garbage_hash)` returns an empty `Vec`, i.e., count of stored-but-unreferenced attachment rows == N, confirming persistent storage of unvalidated attacker-supplied data.
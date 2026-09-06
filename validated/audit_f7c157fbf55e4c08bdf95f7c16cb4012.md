### Title
Atlas attachment downloader stores unrequested/unverified peer content as "instantiated", letting any peer plant attacker data servable via `GetAttachment` RPC - (File: `stackslib/src/net/atlas/download.rs`, `stackslib/src/net/atlas/db.rs`)

### Summary
`AttachmentsDownloader::run` calls `AtlasDB::insert_instantiated_attachment` for every `Attachment` object returned by a sync peer, without first verifying that its self-computed hash (`attachment.hash()`) equals the content hash that was actually solicited from a confirmed, on-chain `AttachmentInstance`. `AtlasDB::find_attachment` (`db.rs:641-648`) later serves any row with `was_instantiated = 1` purely by hash lookup, with no cross-check against `attachment_instances`, so a hostile sync peer can plant arbitrary bytes under a hash of its own choosing and have them served to any unauthenticated RPC caller who knows that hash.

### Finding Description
The claimed equality — "bytes returned for `attachment_hash` == bytes committed by a confirmed name operation" — is broken.

In `AttachmentsDownloader::run` (`download.rs:152-169`):
```
for attachment in context.attachments.drain() {
    let attachments_instances = network.atlasdb.find_all_attachment_instances(&attachment.hash())?;
    network.atlasdb.insert_instantiated_attachment(&attachment)?;
    for attachment_instance in attachments_instances.into_iter() {
        resolved_attachments.push((attachment_instance, attachment.clone()));
    }
    ...
}
```
`insert_instantiated_attachment` (`db.rs:576-592`) is invoked unconditionally, even when `find_all_attachment_instances(&attachment.hash())` returns an empty vector (i.e., no confirmed `attachment_instances` row references this content's hash). The lookup key used to decide "did anyone commit this?" is `attachment.hash()`, which is simply the SHA of the bytes the peer chose to hand back — it is never checked against the content hash that the batch actually requested from that peer (`AttachmentsBatch` tracks `content_hash` values derived from real, on-chain `AttachmentInstance`s, but the downloaded payload's own hash is what's used to key the insert, not the requested hash). A malicious/misbehaving sync peer can therefore respond to any attachment fetch with arbitrary bytes; those bytes get hashed, and the `(hash, content)` pair is written into `attachments` with `was_instantiated = 1` regardless of whether that hash matches any row in `attachment_instances`.

`AtlasDB::find_attachment` (`db.rs:641-648`) has no defense against this: it only checks `was_instantiated = 1`, not whether the hash is linked to a `Checked` `attachment_instances` row. `RPCGetAttachmentRequestHandler::try_handle_request` (`getattachment.rs:93-130`) turns an attacker-guessable/known `Hash160` into exactly this query and returns whatever content is stored, with no re-verification against `attachment_instances`.

### Impact Explanation
An unprivileged remote party that gets selected as an outbound Atlas sync peer (a normal, permissionless role — any node can be an outbound peer) can poison a victim's local `attachments` table with content that no BNS name operation ever committed. Any other unauthenticated RPC client that later requests that hash via `/v2/attachments/:hash` receives HTTP 200 with the attacker's bytes as if they were validated, on-chain-linked zonefile/attachment data. This matches the "attachment/BNS mismatch — state served that no canonical block committed" High-severity category. It is repeatable per attacker-chosen hash and requires no privileged role, secret, or admin access — only running an ordinary peer that another node syncs Atlas data from.

### Likelihood Explanation
Preconditions: the attacker's peer must be selected by the victim as one of its outbound Atlas sync peers (`network.get_outbound_sync_peers()`), which is normal unauthenticated peer behavior in this gossip network. The attacker only needs to respond to a `GetAttachment`-style request from the victim during batch resolution with content of its choosing; no signature or additional check on the delivered `Attachment` is performed before it is persisted as "instantiated." This is low-cost and repeatable for every batch download the victim performs against that peer.

### Recommendation
In `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `find_all_attachment_instances(&attachment.hash())` (or an equivalent check against `attachment_instances` with `status = Checked`) returns at least one matching, confirmed instance. Discard/quarantine attachments whose hash has no corresponding on-chain-committed `attachment_instances` row instead of writing them into `attachments` as instantiated, and consider penalizing/deprioritizing peers that return content for content-hashes they were not asked to resolve.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/download.rs` (or an integration test alongside existing `insert_instantiated_attachment` tests in `stackslib/src/net/api/tests/mod.rs`):
1. Construct an `AtlasDB` with no `attachment_instances` rows (simulating no on-chain commitment).
2. Build an `Attachment` from arbitrary attacker bytes not tied to any instance, call `atlasdb.insert_instantiated_attachment(&attacker_attachment)` directly (mirroring what `download.rs:161` does unconditionally after a peer response).
3. Confirm `atlasdb.find_all_attachment_instances(&attacker_attachment.hash())` is empty (no committing instance exists) yet the row was still inserted with `was_instantiated = 1`.
4. Invoke `RPCGetAttachmentRequestHandler::try_handle_request` with `Hash160::from_hex(&attacker_attachment.hash().to_hex())` and assert the handler returns 200 with `GetAttachmentResponse.attachment == attacker_attachment.content`, despite no `attachment_instances` row ever having referenced that hash.
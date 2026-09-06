### Title
`AtlasDB::insert_instantiated_attachment` persists attacker-supplied attachment content without verifying it matches any on-chain `AttachmentInstance.content_hash`, or capping storage - ([File: stackslib/src/net/atlas/db.rs])

### Summary
`insert_instantiated_attachment` (stackslib/src/net/atlas/db.rs:576-592) unconditionally writes any `Attachment` into the `attachments` table with `was_instantiated=1`, and its only caller in `AttachmentsDownloader::run` (stackslib/src/net/atlas/download.rs:153-169) calls it for every attachment returned by a peer regardless of whether `find_all_attachment_instances(&attachment.hash())` returned anything. Because peer responses to `GetAttachment` are never checked against the requested `content_hash`, a malicious sync peer can get arbitrary content permanently stored, unbounded, with no consensus commitment ever matching it.

### Finding Description
The broken equality: bytes stored under `attachment.hash()` in the `attachments` table are assumed to correspond to some on-chain `AttachmentInstance.content_hash`, but nothing enforces this.

Trace:
1. `AttachmentsBatchStateContext::extend_with_attachments` (download.rs:530-558) takes each HTTP response for an `AttachmentRequest`, calls `response.decode_atlas_get_attachment()` (getattachment.rs:159-165), and blindly inserts `response.attachment` into `self.attachments: HashSet<Attachment>`. `decode_atlas_get_attachment` only JSON-deserializes the body into `GetAttachmentResponse{attachment}` — it never checks that `attachment.hash()` equals the `content_hash` that was actually requested (`AttachmentRequest.content_hash`, download.rs).
2. When the state machine reaches `Done`, `AttachmentsDownloader::run` (download.rs:153-169) drains `context.attachments` and, for every entry, calls `network.atlasdb.find_all_attachment_instances(&attachment.hash())` and then unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` — the emptiness of `attachments_instances` is never checked before persisting.
3. `insert_instantiated_attachment` (db.rs:576-592) does `INSERT OR REPLACE INTO attachments (...) VALUES (?, ?, 1, ?)` keyed only by the attacker-controlled content's own hash, with no size/count cap and no eviction path — unlike `insert_uninstantiated_attachment`, which is bounded by `max_uninstantiated_attachments`/eviction of oldest entries.

A malicious peer selected as an outbound sync target answers a `GetAttachment` request for some legitimate `content_hash` with unrelated, self-consistent (but attacker-chosen) content. The response decodes fine (its own `hash()` need not match anything requested), gets added to `context.attachments`, and is persisted permanently as `was_instantiated=1`, even though `find_all_attachment_instances` for that fabricated hash returns empty (no `AttachmentInstance` row ever pointed to it). Repeating this with unique payloads causes unbounded growth of the `attachments` table with data that no confirmed BNS/contract-call operation ever committed to.

### Impact Explanation
Any malicious/compromised peer chosen for outbound Atlas attachment sync can force a victim node to permanently store arbitrary attacker-chosen blobs in its `attachments` table, marked as validated (`was_instantiated=1`), with zero linkage to any on-chain commitment. This is repeatable per distinct payload and unbounded (no cap/eviction), leading to unbounded disk growth on the victim node — an "attachment/BNS mismatch" causing storage state divergent from anything chain-history committed to. This matches the High severity category (BNS attachment storage growth unbounded by chain state).

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to be selected as a normal outbound Atlas-sync peer (via ordinary peer discovery, no privileged role, no secret), and to have at least one outstanding `AttachmentRequest` sent to it (which happens organically whenever any `AttachmentInstance` referencing a missing attachment is queued and this peer's inventory claims to have it). The attacker's cost is trivial — respond to `GetAttachment` with arbitrary well-formed JSON `GetAttachmentResponse`. The exploit is fully repeatable with a fresh payload each time to keep growing the store, and requires no compromise of secrets, keys, or admin roles.

### Recommendation
- In `AttachmentsBatchStateContext::extend_with_attachments`, verify that `response.attachment.hash()` equals the requested `AttachmentRequest.content_hash` before inserting into `context.attachments`; discard/penalize the peer otherwise.
- In `AttachmentsDownloader::run`, only call `insert_instantiated_attachment` when `attachments_instances` (or an equivalent "there exists a matching on-chain instance") is non-empty; otherwise drop the attachment.
- Add a cap/eviction policy to `insert_instantiated_attachment`/the `attachments` table analogous to `max_uninstantiated_attachments`/`evict_k_oldest_uninstantiated_attachments`, so instantiated attachments cannot grow unbounded even if a future bypass is found.

### Proof of Concept
Rust test in `stackslib::net::atlas::download` (or `db`) tests module:
1. Construct an `AtlasDB` with no matching `AttachmentInstance` rows.
2. Simulate `AttachmentsDownloader::run` completing a batch where `context.attachments` contains N `Attachment` values with unique attacker-chosen `content` (each producing a distinct `hash()` not present in `attachment_instances`).
3. After `run()` (or by directly calling `atlasdb.find_all_attachment_instances(&attachment.hash())` to assert it is empty, then `atlasdb.insert_instantiated_attachment(&attachment)`), query `SELECT COUNT(rowid) FROM attachments WHERE was_instantiated = 1` and assert the count equals N, growing without bound as N increases and without any corresponding `attachment_instances` row referencing those hashes — demonstrating unauthenticated, chain-state-unlinked persistent storage growth.
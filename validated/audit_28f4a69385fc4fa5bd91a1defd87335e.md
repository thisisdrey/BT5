### Title
BNS Attachment Hash Mismatch Not Verified in `AttachmentsBatchStateContext::extend_with_attachments` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When the Atlas attachment downloader receives a `GetAttachmentResponse` for an outbound `AttachmentRequest`, it never checks that the returned `Attachment.content` actually hashes to the `content_hash` that was requested. Any peer that answers a `GET /v2/attachments/{content_hash}` request can therefore supply arbitrary bytes and have them accepted as if they matched the requested hash.

### Finding Description
`AttachmentRequest` carries a `content_hash: Hash160` field that identifies the attachment being fetched, built in `AttachmentRequest::make_request_type`/`StacksHttpRequest::new_getattachment` (`stackslib/src/net/api/getattachment.rs:145-155`). On the response side, `StacksHttpResponse::decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:158-165`) only deserializes the JSON body into a `GetAttachmentResponse { attachment }` — it performs no comparison between `Hash160::from_data(&attachment.content)` and any expected hash.

In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`), for each successfully-completed `(request, response)` pair, the code calls `response.decode_atlas_get_attachment()` and, on success, does:
```
self.attachments.insert(response.attachment);
```
There is no line comparing `response.attachment.hash()` (or an equivalent `Hash160::from_data` call) against `request.content_hash`. The `request` value, which does carry the original `content_hash` (set at `download.rs:467-472`), is only used to look up the peer's reliability report (`request.get_url()`), never to validate the payload.

Because the request path is a plain HTTP GET keyed only by content hash, and the response is accepted from any peer that was in the "sources" set (built from that peer's advertised attachment inventory, not any authenticated commitment), a malicious peer that is selected as a download source can return any `content` bytes it likes in the JSON body, and those bytes will be inserted into `self.attachments` unconditionally.

### Impact Explanation
This allows a remote, unprivileged peer that is contacted for an attachment download to serve arbitrary attacker-chosen bytes as "the" attachment for a given BNS name/zonefile hash. If this attachment set is subsequently committed to `AtlasDB`/used to resolve BNS zonefiles, the node ends up serving non-canonical data as if it were the canonical zonefile content committed on-chain — matching the "High: serving non-canonical state as canonical / attachment/BNS mismatch" impact category. It is repeatable per attachment request the node issues.

### Likelihood Explanation
No special privileges are needed: any peer that a node considers a download source (i.e., a peer whose inventory claims to have the attachment) can be queried and can return a forged response. This requires only that the node choose the attacker's peer as a source, which happens whenever the attacker legitimately runs a P2P peer and gossips an inventory bit claiming to hold the attachment — a normal, unprivileged interaction.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:547-552`), after decoding the response, verify `response.attachment.hash() == request.content_hash` (using `Attachment::hash()` / `Hash160::from_data`) before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and optionally penalize/deregister the offending peer.

### Proof of Concept
Rust test in `stackslib/src/net/atlas/tests.rs` (or a new test module):
1. Construct an `AttachmentsBatchStateContext` with a single peer and a pending `AttachmentRequest { content_hash: H1, .. }`.
2. Build a crafted `StacksHttpResponse` whose JSON body is `GetAttachmentResponse { attachment: Attachment { content: b"evil".to_vec() } }`, where `Hash160::from_data(b"evil") != H1`.
3. Put `(request, Some(response))` into `results.succeeded` and call `context.extend_with_attachments(&mut results)`.
4. Assert that `context.attachments` contains the "evil" `Attachment` despite `Hash160::from_data(&attachment.content) != H1`, proving the missing hash-equality check.
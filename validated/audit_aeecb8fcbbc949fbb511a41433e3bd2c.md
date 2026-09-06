### Title
Unvalidated `GetAttachmentsInvResponse.block_id`/`pages[].index` trusted in attachment-inventory matching - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::get_prioritized_attachments_requests` looks up per-peer inventory pages using `response.pages.iter().find(|page| page.index == page_index)` at `download.rs:437`, trusting attacker-supplied `AttachmentPage.index`/`inventory` fields from `GetAttachmentsInvResponse` without ever checking `response.block_id` against the request's `index_block_hash`. The response is stored in `self.inventories` keyed by the *request* tuple `(contract_id, pages, index_block_hash)` — never by anything derived from the response's own claimed `block_id` — so a malicious peer's self-declared `block_id` is not cross-verified with what was actually requested.

### Finding Description
The request/response protocol for Atlas attachment inventories is defined in `RPCGetAttachmentsInvRequestHandler` (`stackslib/src/net/api/getattachmentsinv.rs:135-218`), which returns a `GetAttachmentsInvResponse { block_id, pages }` where `block_id` is simply echoed from the query string server-side and `pages[i].index` mirrors the requested page indexes. On the client (downloader) side, `AttachmentsBatchStateContext` stores incoming responses in `self.inventories: HashMap<(QualifiedContractIdentifier, Vec<u32>, StacksBlockId), HashMap<UrlString, GetAttachmentsInvResponse>>` (`download.rs:347-350`), where the outer key is derived from the *request* (`AttachmentsInventoryRequest::key()`, `download.rs:1006-1013`), not from anything in the response payload.

When attachments are prioritized for fetch, `get_prioritized_attachments_requests` iterates `self.inventories` by request key `(contract_id, pages, index_block_hash)` (`download.rs:407`), then for each peer's stored `response` performs:
```
let search_page = response.pages.iter().find(|page| page.index == page_index);
```
at `download.rs:437`. This code path never inspects `response.block_id` at all, and only matches on `page.index`, an attacker-controlled field inside the JSON body. Since a queried peer is untrusted and can be any remote node returning a "200 OK" `GetAttachmentsInvResponse` body, it can:
1. Return `block_id` unrelated to the actual `index_block_hash` that was requested (no check exists anywhere in this path comparing `response.block_id` to the key's `index_block_hash`).
2. Return `pages[i].index` values that happen to equal the real `page_index` being searched for, with a forged `inventory` bit vector.

Because the response is stored keyed solely by the *outgoing request's* tuple and consumed via `.find(|page| page.index == page_index)` with no `block_id` equality check, the downloader treats the forged (possibly-for-a-different-tip) page inventory bits as authoritative for the real page being evaluated at the real `index_block_hash`.

### Impact Explanation
A malicious peer can cause the node to derive a false `has_attachment` decision (`download.rs:439-444`) for a specific attachment at a specific canonical `index_block_hash`, either:
- Falsely reporting the bit as `0`, causing the node to skip fetching from otherwise-honest peers if the false response biases source selection or exhausts peers considered, or
- Falsely reporting the bit as `1` for an attachment the peer doesn't actually have, wasting a fetch attempt against that peer for `AttachmentRequest` (built at `download.rs:466-470`), a bounded compute/bandwidth cost to the requester, not the network.

This matches the "attachment/BNS mismatch" High-impact category: attacker steers Atlas attachment-inventory bookkeeping using data that was never validated against the requested tip, though the fetched attachment content itself is still separately hash-verified elsewhere in the Atlas pipeline (`AttachmentInstance`/content-hash checks), limiting this to inventory-bit spoofing rather than serving forged attachment bytes as canonical.

### Likelihood Explanation
Any queried peer in `self.peers` (an unprivileged remote peer that answered `/v2/attachments/inv`) can trigger this on every attachments-inv round for any batch it's asked about — no signature, secret, or privileged role is required, and the response is standard JSON over the already-established HTTP RPC endpoint. Attacker cost is a single crafted HTTP response with arbitrary `block_id` and `pages[].index`/`inventory` values.

### Recommendation
In `get_prioritized_attachments_requests` (`download.rs:404-...`), before using `response.pages`, verify `response.block_id == index_block_hash` (the third element of the key tuple) and that each consulted `page.index` is a member of the originally-requested `pages` for that key; discard/ignore (and optionally penalize the peer's `ReliabilityReport`) responses that fail this check rather than trusting `.find()` blindly.

### Proof of Concept
Add a unit test in `stackslib/src/net/atlas/tests.rs` (or a dedicated `download.rs` test) that:
1. Builds an `AttachmentsBatchStateContext` with one peer and one `AttachmentsInventoryRequest` for `index_block_hash = A`, `pages = [0]`.
2. Manually inserts into `context.inventories` under key `(contract_id, vec![0], A)` a `GetAttachmentsInvResponse { block_id: B /* != A */, pages: vec![AttachmentPage{ index: 0, inventory: vec![1] }] }` for that peer.
3. Calls `context.get_prioritized_attachments_requests()`.
4. Asserts the returned queue does **not** include a request sourced from that peer (expected fix behavior) — currently it will incorrectly include it, since `response.block_id` (`B`) is never compared to `A`, demonstrating the broken equality at `download.rs:437`.
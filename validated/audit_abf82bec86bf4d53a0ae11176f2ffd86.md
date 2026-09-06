### Title
Peer-forged `block_id` in `GetAttachmentsInvResponse` is never checked against the requested `index_block_hash` before being trusted as attachment inventory - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_inventories` decodes a peer's `GetAttachmentsInvResponse` and stores it keyed only by `request.key()` (derived from the locally-known `contract_id`/`pages`/`index_block_hash` of the *request*), without ever comparing the response's own `block_id` field to the `index_block_hash` that was actually queried. `get_prioritized_attachments_requests` subsequently iterates these stored responses and trusts their `pages`/`inventory` bits directly to decide which peers are "sources" for a missing attachment, again with no `block_id` cross-check.

### Finding Description
In `extend_with_inventories` (download.rs:490-528), for each successfully-decoded response:
```
if let Ok(response) = response.decode_atlas_attachments_inv_response() {
    let peer_url = request.get_url().clone();
    match self.inventories.entry(request.key()) { ... insert(peer_url, response) ... }
``` [1](#0-0) 

The map is keyed by `request.key()` (built from the outbound `AttachmentsInventoryRequest`, which carries `contract_id`, `pages`, and `index_block_hash`), not by anything derived from the response. The decoded `response` (a `GetAttachmentsInvResponse`, which carries its own `block_id` field claimed by the remote peer) is inserted verbatim with no assertion that `response.block_id == request.index_block_hash`.

Later, `get_prioritized_attachments_requests` (download.rs:404-478) iterates `self.inventories` and, for each peer's stored response, searches `response.pages` for the wanted `page_index` and reads the inventory bit at `position_in_page` to decide whether that peer is a candidate source:
```
let search_page = response.pages.iter().find(|page| page.index == page_index);
let has_attachment = search_page...
if !has_attachment { continue; }
...
sources.insert(peer_url.clone(), report.clone());
``` [2](#0-1) 

At no point in either function is `response.block_id` read or compared to `self.attachments_batch.index_block_hash` / `request.index_block_hash`. A malicious peer can therefore answer a `GetAttachmentsInv` request for `index_block_hash = IBH1` with a `GetAttachmentsInvResponse{ block_id: IBH2, pages: [...] }` containing arbitrary/forged inventory bits, and the response will be accepted, stored, and used to select that peer as an attachment "source" exactly as if it had legitimately reported inventory for IBH1.

### Impact Explanation
The scope of damage is bounded: `get_prioritized_attachments_requests` only uses the forged inventory bits to decide *which peer to ask* for a given `content_hash`; the `content_hash` itself and the target attachment identity come from the node's own locally-known `attachments_instances` (chain-derived), not from the peer's response. The vulnerability does not let an attacker inject arbitrary attachment bytes past hash verification, since actual attachment content is still validated against the pre-committed `content_hash` when received. The concrete effect of the missing `block_id` check is that a peer can claim inventory availability for a block it was never asked about, causing the downloader to select it as a source based on unrelated/fabricated data — a form of attachment inventory/state mismatch, but it does not directly result in forged canonical data being stored, a crash, or an authentication bypass.

### Likelihood Explanation
The check is trivially exploitable by any peer already selected as an outbound sync peer for the node's Atlas attachment fetching (no special privilege beyond running an ordinary P2P peer with a reachable data URL), and is repeatable on every `AttachmentsInventoryRequest`/response round.

### Recommendation
In `extend_with_inventories`, after `decode_atlas_attachments_inv_response()` succeeds, explicitly verify `response.block_id == request.index_block_hash` before inserting into `self.inventories`; treat a mismatch as a failed/faulty response (`report.bump_failed_requests()` and/or mark the peer as faulty) rather than silently accepting it.

### Proof of Concept
Construct an `AttachmentsInventoryRequest` with `index_block_hash = IBH1`; construct a `BatchedRequestsResult` whose `succeeded` map pairs that request with an HTTP response that decodes to `GetAttachmentsInvResponse { block_id: IBH2, pages: vec![AttachmentPage{ index: 0, inventory: vec![1] }] }`; call `AttachmentsBatchStateContext::extend_with_inventories` and assert `self.inventories` contains the entry keyed by `request.key()` holding the `IBH2`-tagged response; then call `get_prioritized_attachments_requests` and assert the corresponding peer is included in `sources` for the missing attachment, despite `block_id != index_block_hash`.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L434-458)
```rust
                for (peer_url, response) in peers_responses.iter() {
                    // Considering the response, look for the page with the index
                    // we're looking for.
                    let search_page = response.pages.iter().find(|page| page.index == page_index);

                    let has_attachment = search_page
                        .and_then(|search_page| {
                            search_page.inventory.get(position_in_page as usize)
                        })
                        .map(|result| *result == 1)
                        .unwrap_or(false);

                    if !has_attachment {
                        debug!(
                            "Atlas: peer does not have attachment ({}, {}) in its inventory {:?}",
                            page_index, position_in_page, response.pages
                        );
                        continue;
                    }

                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
```

**File:** stackslib/src/net/atlas/download.rs (L507-518)
```rust
            if let Ok(response) = response.decode_atlas_attachments_inv_response() {
                let peer_url = request.get_url().clone();
                match self.inventories.entry(request.key()) {
                    Entry::Occupied(responses) => {
                        responses.into_mut().insert(peer_url, response);
                    }
                    Entry::Vacant(v) => {
                        let mut responses = HashMap::new();
                        responses.insert(peer_url, response);
                        v.insert(responses);
                    }
                };
```

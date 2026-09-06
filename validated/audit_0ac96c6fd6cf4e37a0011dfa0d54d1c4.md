### Title
GET /v2/attachments/inv serves attachment inventory for non-canonical (orphaned) blocks without any canonical-tip check - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`RPCGetAttachmentsInvRequestHandler::try_handle_request` takes an attacker-supplied `index_block_hash` and passes it directly to `AtlasDB::get_attachments_available_at_page_index` with no check that the block is on the canonical fork. Any `index_block_hash` for which `attachment_instances` rows exist — including blocks that were later orphaned — returns a 200 OK with populated inventory bits.

### Finding Description
The equality that should hold is: *inventory served for a given `index_block_hash` == attachment-availability state that was actually committed on the canonical fork at that block*. The handler in `getattachmentsinv.rs:135-218` parses `index_block_hash` from the query string with zero validation beyond hex-decoding [1](#0-0) , then for each requested page directly calls `network.get_atlasdb().get_attachments_available_at_page_index(*page_index, &index_block_hash)` [2](#0-1) . That function and its helper `get_attachments_missing_at_page_index` in `db.rs:471-509` run a raw SQL query filtering `attachment_instances` solely `WHERE ... AND index_block_hash = ?3`, with no join or check against the chainstate's canonical `index_block_hash`/tip [3](#0-2) . There is no call anywhere in this path to resolve or compare against the canonical tip (e.g. no `chainstate.get_stacks_chain_tip` or `SortitionDB` canonical check). Rows in `attachment_instances` are keyed by whatever `index_block_hash` was associated with a block at the time it was processed by the node (including blocks that can later become orphaned by a fork), so once such a row exists, it remains queryable forever with no canonical-tip gating. Since the only requirement is that the hex-decoded `StacksBlockId` matches some row previously inserted, the response returns whatever inventory bits are set for that (possibly orphaned) block — the 404 path (`HttpNotFound`) is only reached on DB errors, not on non-canonical lookups.

### Impact Explanation
A remote unprivileged client can query `/v2/attachments/inv` with the `index_block_hash` of any orphaned/non-canonical block for which the node once processed attachment instances, and receive a 200 response with real inventory bits, i.e. state that was never committed on the canonical fork is served as if it were current/canonical. This matches the "High: serving non-canonical state as canonical" category — it can mislead Atlas attachment-inventory consumers/peers about what is available at a given block, and could be leveraged to steer clients into requesting/trusting attachment data tied to a fork that isn't the current tip.

### Likelihood Explanation
The precondition is simply that the node has, at some point, processed and recorded `attachment_instances` for a block that subsequently became non-canonical (an orphan/fork event) — a normal occurrence in blockchain operation, not requiring any special access. The attacker needs no secret, no privileged role, and no P2P handshake — this is a single unauthenticated HTTP GET to the public RPC port, fully repeatable at will.

### Recommendation
Before serving the inventory, resolve `index_block_hash` against the canonical chain (e.g., verify it equals or is an ancestor consistent with `SortitionDB`/`StacksChainState`'s canonical stacks tip) and return 404 if the block is not canonical, mirroring how other endpoints validate `index_block_hash` against the canonical tip.

### Proof of Concept
In `stackslib/src/net/api/getattachmentsinv.rs` tests (or `net/atlas/tests.rs`), using the `TestRPC` harness:
1. Insert `attachment_instances` rows directly into `AtlasDB` keyed by a `StacksBlockId` that is deliberately never linked to the canonical `StacksChainState`/`SortitionDB` tip (e.g., a hash from a block that is fabricated/orphaned in the test harness).
2. Issue `RPCGetAttachmentsInvRequestHandler` (or a full `GET /v2/attachments/inv?index_block_hash=<orphan-hash>&pages_indexes=0`) request against that harness.
3. Assert the response is `200 OK` with a `GetAttachmentsInvResponse` whose `pages[0].inventory` reflects the inserted (non-canonical) rows, instead of a `404`/empty result — confirming `get_attachments_available_at_page_index` (db.rs:471-483) is queried with no canonical-tip gate.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L87-110)
```rust
        let mut index_block_hash = None;
        let mut page_indexes = HashSet::new();

        // expect index_block_hash= and page_indexes=
        for (key, value) in form_urlencoded::parse(query_str.as_bytes()) {
            if key == "index_block_hash" {
                index_block_hash = StacksBlockId::from_hex(&value).ok();
            } else if key == "pages_indexes" {
                let pages_indexes_value = value.to_string();
                for entry in pages_indexes_value.split(',') {
                    if let Ok(page_index) = entry.parse::<u32>() {
                        page_indexes.insert(page_index);
                    }
                }
            }
        }

        let index_block_hash = if let Some(ibh) = index_block_hash {
            ibh
        } else {
            return Err(Error::DecodeError(
                "Invalid Http request: expecting index_block_hash".to_string(),
            ));
        };
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L179-196)
```rust
        for page_index in page_indexes.iter() {
            let page_res =
                node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                    match network
                        .get_atlasdb()
                        .get_attachments_available_at_page_index(*page_index, &index_block_hash)
                    {
                        Ok(inventory) => Ok(AttachmentPage {
                            inventory,
                            index: *page_index,
                        }),
                        Err(e) => {
                            let msg = format!("Unable to read Atlas DB - {}", e);
                            warn!("{}", msg);
                            Err(msg)
                        }
                    }
                });
```

**File:** stackslib/src/net/atlas/db.rs (L485-509)
```rust
    pub fn get_attachments_missing_at_page_index(
        &self,
        page_index: u32,
        block_id: &StacksBlockId,
    ) -> Result<Vec<bool>, db_error> {
        let min = page_index
            .checked_mul(AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE)
            .ok_or(db_error::Overflow)?;
        let max = min
            .checked_add(AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE)
            .ok_or(db_error::Overflow)?;
        let qry = "SELECT attachment_index, is_available FROM attachment_instances WHERE attachment_index >= ?1 AND attachment_index < ?2 AND index_block_hash = ?3 ORDER BY attachment_index ASC";
        let args = params![min, max, block_id,];
        let rows = query_rows::<(u32, u32), _>(&self.conn, qry, args)?;

        let mut bool_vector = vec![true; AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE as usize];
        for (attachment_index, is_available) in rows.into_iter() {
            let index = attachment_index % AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;
            let slot = bool_vector
                .get_mut(index as usize)
                .ok_or(db_error::NotFoundError)?;
            *slot = is_available == 0;
        }
        Ok(bool_vector)
    }
```

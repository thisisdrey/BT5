### Title
Unbounded per-page SQL scan cost in `GET /v2/attachments/inv` allows compute DoS scaling with `attachment_instances` table size - ([File: stackslib/src/net/api/getattachmentsinv.rs])

### Summary
`RPCGetAttachmentsInvRequestHandler::try_handle_request` caps the number of pages per request at `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8), but each page lookup executes `AtlasDB::get_attachments_missing_at_page_index`, whose SQL query filters on `attachment_index` range and `index_block_hash` equality with no proven covering index. An unauthenticated attacker can repeatedly request 8 pages against any attacker-chosen `index_block_hash`, forcing the node to repeatedly evaluate this predicate over the entire `attachment_instances` table, which grows unboundedly over the life of the chain since there is no eviction for that table (unlike the `attachments` content table, which is bounded by `max_uninstantiated_attachments`).

### Finding Description
The handler decodes `index_block_hash` and up to 8 `pages_indexes` from the query string with no authentication (`security: []` per the OpenAPI spec), then loops over each page index calling `network.get_atlasdb().get_attachments_available_at_page_index(*page_index, &index_block_hash)` [1](#0-0) . This delegates to `get_attachments_missing_at_page_index`, which runs:
```
SELECT attachment_index, is_available FROM attachment_instances
WHERE attachment_index >= ?1 AND attachment_index < ?2 AND index_block_hash = ?3
ORDER BY attachment_index ASC
``` [2](#0-1) 

The code comment in the handler itself explicitly acknowledges the missing safeguard: "We could also add the notion of 'budget' so that a client could only get a limited number of pages when they are spanning over many blocks" [3](#0-2) . The only guard present is the page-count cap (`page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`) [4](#0-3) , which bounds the number of queries per request but does nothing to bound the cost of each individual query, which is a function of how the SQL engine resolves the `WHERE` predicate against the full `attachment_instances` table content, not of the constant-size result window (`ATTACHMENTS_INV_PAGE_SIZE` = 8 rows).

Unlike the `attachments` uninstantiated-content table, which is actively bounded and evicted (`evict_k_oldest_uninstantiated_attachments`, capped by `max_uninstantiated_attachments`) [5](#0-4) , there is no equivalent eviction visible for the `attachment_instances` metadata table that backs this query, so it can grow proportionally to the total history of attachment activity tracked by Atlas across all contracts and blocks over the node's lifetime.

Since `index_block_hash` is attacker-controlled and unauthenticated, and the request can be repeated arbitrarily, an attacker can force the node to repeatedly execute 8 range/equality-filtered queries per request against a large `attachment_instances` table, with per-request cost that grows with total table size rather than with the fixed 8-page/64-bit result window the API nominally returns.

### Impact Explanation
Any remote, unauthenticated party can send repeated `GET /v2/attachments/inv` requests, each triggering up to 8 SQL queries against the `attachment_instances` table. As the table grows with normal network operation (accumulated attachment instances across contracts and blocks, with no apparent bounding/eviction), the per-request compute cost for this endpoint increases without any cap tied to the response size, allowing a bounded compute DoS on this specific read endpoint — matching the "bounded compute DoS on a read endpoint" High-severity category. This does not crash the node or affect consensus state, but it can degrade the node's RPC responsiveness under repeated attacker requests.

### Likelihood Explanation
- The `/v2/attachments/inv` endpoint requires no authentication (`security: []`), is remotely reachable over the RPC port, and requires no privileged role, secret, or peer state to invoke.
- The attacker only needs any valid-looking `index_block_hash` (does not need to correspond to a real/canonical block — the query simply returns "not found" defaults if unmatched, but the table scan cost is paid regardless of match) and up to 8 arbitrary page indices.
- The attack is trivially repeatable and requires no established peer or StackerDB relationship — a single unauthenticated HTTP client suffices.
- The severity of the impact scales with how large `attachment_instances` grows in practice on a long-running mainnet node, which is an operational condition rather than something the attacker directly controls, tempering the severity somewhat but not eliminating it.

### Recommendation
Add a query plan/index that allows `get_attachments_missing_at_page_index` to resolve its predicate without scanning the full `attachment_instances` table (e.g., ensure a composite index/covering index on `(index_block_hash, attachment_index)` if one does not already exist), and/or introduce a per-request or per-IP rate limit / cost budget for `/v2/attachments/inv` independent of the page-count cap, so that repeated queries cannot be used to amplify database scan cost against attacker-chosen `index_block_hash` values.

### Proof of Concept
Rust net test plan (in `stackslib/src/net/atlas/tests.rs` or `stackslib/src/net/api/tests/getattachmentsinv.rs`):
1. Populate an in-memory `AtlasDB` with a small number of `attachment_instances` rows (e.g., 100) and separately with a large number (e.g., 1,000,000), using `queue_attachment_instance` / `mark_attachment_instance_checked`.
2. For each DB, invoke `RPCGetAttachmentsInvRequestHandler::try_handle_request` (or directly `AtlasDB::get_attachments_missing_at_page_index`) with 8 page indices against a fixed `index_block_hash`, timing the call.
3. Assert that latency for the large-table case is not bounded by a constant factor of the small-table case (i.e., cost scales with table size), demonstrating the lack of a per-request cost ceiling independent of `attachment_instances` table size, at the exact call site `stackslib/src/net/atlas/db.rs:496-498`.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L155-158)
```rust
        // Since clients can be asking for non-consecutive pages indexes (1, 5_000, 10_000, ...),
        // we will be handling each page index separately.
        // We could also add the notion of "budget" so that a client could only get a limited number
        // of pages when they are spanning over many blocks.
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L159-168)
```rust
        if page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST {
            let msg = format!(
                "Number of attachment inv pages is limited by {} per request",
                MAX_ATTACHMENT_INV_PAGES_PER_REQUEST
            );
            warn!("{msg}");
            return StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new(msg))
                .try_into_contents()
                .map_err(NetError::from);
        }
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

**File:** stackslib/src/net/atlas/db.rs (L511-536)
```rust
    pub fn insert_uninstantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        // Insert the new attachment
        let uninstantiated_attachments = self.count_uninstantiated_attachments()?;
        if uninstantiated_attachments >= self.atlas_config.max_uninstantiated_attachments {
            let to_delete =
                1 + uninstantiated_attachments - self.atlas_config.max_uninstantiated_attachments;
            self.evict_k_oldest_uninstantiated_attachments(to_delete)?;
        }

        let tx = self.tx_begin()?;
        let now = util::get_epoch_time_secs() as i64;
        let res = tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 0, ?)",
            params![
                attachment.hash(),
                attachment.content,
                now,
            ],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }
```

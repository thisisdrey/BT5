### Title
Atlas attachment content de-duplicated by hash can be downgraded to "uninstantiated" and evicted, breaking availability for all `AttachmentInstance`s that reference it - (`stackslib/src/net/atlas/db.rs`)

### Summary
The Atlas subsystem stores attachment payloads in a single `attachments` table keyed uniquely by content hash, and this content row is referenced by many independent `attachment_instances` rows (potentially from different contracts/blocks). This is architecturally the same "shared resource keyed by hash" pattern as the Ethermint `CodeHash -> bytecode` bug: a write/eviction operation on the shared entry, driven by one reference, can affect every other reference to the same content.

### Finding Description
The `attachments` table is content-addressed and de-duplicated: [1](#0-0) 

Each `AttachmentInstance` (one per `(index_block_hash, contract_id, attachment_index)`) only stores a `content_hash` pointer into this shared table, so multiple, unrelated instances (from different contracts and different blocks) can legitimately point at the exact same `attachments` row when their content is identical: [2](#0-1) 

`insert_uninstantiated_attachment` unconditionally performs an `INSERT OR REPLACE` that (re)writes the shared row with `was_instantiated = 0`, regardless of whether a row for that hash already exists and was previously marked `was_instantiated = 1` (i.e., already validated/confirmed): [3](#0-2) 

The eviction routine subsequently deletes rows purely based on `was_instantiated = 0`, oldest-first, once the uninstantiated-attachment cap is exceeded: [4](#0-3) 

Because the content row is shared across all instances with the same `content_hash`, downgrading it back to "uninstantiated" (via `insert_uninstantiated_attachment`) makes it eligible for eviction even though other, already-`Checked`/`is_available=1` `attachment_instances` rows still depend on it, e.g. those looked up via `find_all_attachment_instances`/`find_attachment`: [5](#0-4) 

This mirrors the Ethermint flaw's root cause exactly: a shared, hash-keyed resource with no reference counting, where an operation legitimately scoped to one referrer (re-insertion/eviction of "unvalidated" content) silently affects all other referrers of the same hash, breaking the "served == committed/authenticated" equality that `is_available`/`status=Checked` is supposed to guarantee.

### Impact Explanation
If reachable, this breaks the equality between an attachment instance's `is_available`/`Checked` status (committed via on-chain contract calls) and what is actually served from the `attachments` table (`find_attachment`), causing a Stacks node to report an attachment as available while being unable to serve its content — a bounded compute/read-availability DoS on Atlas attachment lookups, consistent with the "High" impact bar (serving non-canonical/mismatched state, degraded read endpoint) defined in scope.

### Likelihood Explanation
I was able to fully confirm the shared-row schema, the unconditional `INSERT OR REPLACE ... was_instantiated=0` semantics of `insert_uninstantiated_attachment`, and the `was_instantiated=0`-based eviction logic, all within `stackslib/src/net/atlas/db.rs`. I was **not able to conclusively trace, within the exploration performed, the exact network-facing call site** that invokes `insert_uninstantiated_attachment` with attacker-influenced/downloaded content (the call graph search returned only its definition and unit-test usages within the indexed content; `stackslib/src/net/p2p.rs` also references `attachment_instances`/`attachments` logic but its call flow was not reviewed in depth due to iteration limits). Because the remote reachability of the downgrade-then-evict path from an unprivileged peer is not fully confirmed, the likelihood should be treated as unverified/moderate rather than proven, and this finding should be validated further (e.g., by inspecting `stackslib/src/net/p2p.rs` and the `AttachmentsDownloader`/HTTP endpoints that ingest attachment content from peers) before being treated as a confirmed exploitable bug.

### Recommendation
- Track reference counts (or simply never downgrade `was_instantiated` once set to `1`) so that a row already confirmed by an on-chain instance cannot be reclassified as evictable "uninstantiated" content.
- Before evicting an "uninstantiated" attachment row, check whether any `attachment_instances` row with `status = Checked AND is_available = 1` still references that `content_hash`, and skip eviction if so.
- Audit all callers of `insert_uninstantiated_attachment` to ensure it is never invoked for a hash that already has a `was_instantiated = 1` row (or make the SQL conditional, e.g. `INSERT OR IGNORE`, or `UPDATE ... SET was_instantiated = MAX(was_instantiated, 0)`-style logic) to avoid downgrading committed content.

### Proof of Concept
Not confirmed end-to-end due to the unresolved call site (see Likelihood Explanation). The reproducible database-level behavior is:
1. Insert an attachment content row and mark it instantiated: `atlas_db.insert_instantiated_attachment(&attachment)` → row has `was_instantiated = 1`.
2. Call `atlas_db.insert_uninstantiated_attachment(&attachment)` with the same content (function unconditionally sets `was_instantiated = 0` via `INSERT OR REPLACE`, per `stackslib/src/net/atlas/db.rs:511-536`).
3. Trigger `evict_k_oldest_uninstantiated_attachments`/`evict_expired_uninstantiated_attachments` — the row is now eligible for deletion despite existing `attachment_instances` rows (from potentially unrelated contracts) marking it `Checked`/`is_available = 1`.
4. Subsequent `find_attachment(&content_hash)` calls (e.g. servicing `/v2/attachments/<hash>`) return `None` for content the node's own `attachment_instances` table claims is available.

Full confirmation that step 2 is reachable from unauthenticated network input requires further review of `stackslib/src/net/p2p.rs` and the Atlas HTTP/P2P ingestion paths, which was not completed within this analysis.

### Citations

**File:** stackslib/src/net/atlas/db.rs (L64-86)
```rust
const ATLASDB_INITIAL_SCHEMA: &[&str] = &[
    r#"
    CREATE TABLE attachments(
        hash TEXT UNIQUE PRIMARY KEY,
        content BLOB NOT NULL,
        was_instantiated INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );"#,
    r#"
    CREATE TABLE attachment_instances(
        content_hash TEXT,
        created_at INTEGER NOT NULL,
        index_block_hash STRING NOT NULL,
        attachment_index INTEGER NOT NULL,
        block_height INTEGER NOT NULL,
        is_available INTEGER NOT NULL,
        metadata TEXT NOT NULL,
        contract_id STRING NOT NULL,
        tx_id STRING NOT NULL,
        PRIMARY KEY(index_block_hash, contract_id, attachment_index)
    );"#,
    "CREATE TABLE db_config(version TEXT NOT NULL);",
];
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

**File:** stackslib/src/net/atlas/db.rs (L538-547)
```rust
    pub fn evict_k_oldest_uninstantiated_attachments(&mut self, k: u32) -> Result<(), db_error> {
        let tx = self.tx_begin()?;
        let res = tx.execute(
            "DELETE FROM attachments WHERE hash IN (SELECT hash FROM attachments WHERE was_instantiated = 0 ORDER BY created_at ASC LIMIT ?)",
            params![k],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }
```

**File:** stackslib/src/net/atlas/db.rs (L630-648)
```rust
    pub fn find_all_attachment_instances(
        &self,
        content_hash: &Hash160,
    ) -> Result<Vec<AttachmentInstance>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT * FROM attachment_instances WHERE content_hash = ?1 AND status = ?2";
        let args = params![hex_content_hash, AttachmentInstanceStatus::Checked];
        let rows = query_rows(&self.conn, qry, args)?;
        Ok(rows)
    }

    pub fn find_attachment(&self, content_hash: &Hash160) -> Result<Option<Attachment>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 1"
            .to_string();
        let args = params![hex_content_hash];
        let row = query_row::<Attachment, _>(&self.conn, &qry, args)?;
        Ok(row)
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L167-181)
```rust
#[derive(Debug, Clone, Serialize, Deserialize, Eq, PartialEq, Hash)]
/// An attachment instance is a reference to atlas data: a commitment
/// to track the content that is the inverse of `content_hash`.
/// Attachment instances are created by atlas events issued by contracts
/// specified in a node's `AtlasConfig`.
pub struct AttachmentInstance {
    pub content_hash: Hash160,
    pub attachment_index: u32,
    pub stacks_block_height: u64,
    pub index_block_hash: StacksBlockId,
    pub metadata: String,
    pub contract_id: QualifiedContractIdentifier,
    pub tx_id: Txid,
    pub canonical_stacks_tip_height: Option<u64>,
}
```

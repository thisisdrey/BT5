### Title
Stale/invalidated Epoch2 PoX fork state served via `tip=` parameter on read-only RPC endpoints - ([File: stackslib/src/chainstate/burn/db/sortdb.rs])

### Summary
`SortitionDB::index_handle_at_block` (stackslib/src/chainstate/burn/db/sortdb.rs:2722-2755), which is invoked by `RPCGetDataVarRequestHandler`, `RPCGetMapEntryRequestHandler`, `RPCGetClarityMarfRequestHandler`, and other read-only Clarity RPC handlers, resolves the burn view for `StacksBlockHeaderTypes::Epoch2` headers using `header.consensus_hash` directly and never checks the resulting sortition snapshot's `pox_valid` flag. This is in contrast to `get_stacks_header_hashes` (sortdb.rs:3761-3774), which explicitly rejects consensus hashes whose snapshot has `pox_valid == false`. As a result, a caller who supplies `tip=<StacksBlockId>` for a Stacks Epoch2 block whose sortition was later invalidated by a PoX reorg can have the node open a `SortitionHandleConn` rooted at a `pox_valid=false` sortition and serve Clarity state from a fork the node itself no longer considers canonical.

### Finding Description
The claimed equality is: *state committed under a PoX-valid canonical fork == state served by read RPCs*. `index_handle_at_block` breaks this equality for Epoch2 headers:

```
// stackslib/src/chainstate/burn/db/sortdb.rs:2743-2754
let burn_view = match &header.anchored_header {
    StacksBlockHeaderTypes::Epoch2(_) => header.consensus_hash,
    StacksBlockHeaderTypes::Nakamoto(_) => header.burn_view.ok_or_else(...)?,
};

let snapshot = SortitionDB::get_block_snapshot_consensus(self.conn(), &burn_view)?
    .ok_or(db_error::NotFoundError)?;
Ok(self.index_handle(&snapshot.sortition_id))
```

No `snapshot.pox_valid` check is performed before constructing and returning the `SortitionHandleConn`. Compare this to the sibling function that resolves consensus hashes into snapshots elsewhere in the same file:

```
// stackslib/src/chainstate/burn/db/sortdb.rs:3768-3774
let tip_snapshot = SortitionDB::get_block_snapshot_consensus(self, tip_consensus_hash)?
    .ok_or_else(|| db_error::NotFoundError)?;

if !tip_snapshot.pox_valid {
    warn!("Consensus hash {:?} corresponds to a sortition that is not on the canonical PoX fork", tip_consensus_hash);
    return Err(db_error::InvalidPoxSortition);
}
```

The read-only RPC handlers (e.g. `RPCGetDataVarRequestHandler::try_handle_request`, stackslib/src/net/api/getdatavar.rs:114-167) accept a caller-controlled `tip` (resolved from the `tip` query parameter via `node.load_stacks_chain_tip`), and pass it straight to `sortdb.index_handle_at_block(chainstate, &tip)?` inside `node.with_node_state(...)`. Because `NakamotoChainState::get_block_header` will still return the stored Epoch2 header for any previously-processed block (headers are not deleted when a PoX anchor block is invalidated — only the sortition snapshot's `pox_valid` bit flips), and because `index_handle_at_block` does not check that bit, the handler happily opens a MARF handle rooted at the stale/invalid fork and serves the data-var/map-entry/marf-value/stacker-db-chunk value as if it were canonical.

### Impact Explanation
An unprivileged remote caller who knows (or can discover) the `StacksBlockId` of an Epoch2 Stacks block that was later orphaned by a PoX reorg can query `/v2/data_var/...`, `/v2/map_entry/...`, or the raw MARF-value endpoint with `?tip=<that block id>` and receive a 200 response with state from the invalidated fork, rather than the expected 404/`InvalidPoxSortition` error. This is a case of "serving non-canonical state as canonical" on a read endpoint — matching the High severity category in the rubric. The impact is repeatable for any block whose header row still exists and whose sortition has since been marked `pox_valid = false`; every one of the four listed handlers (and any other consumer of `index_handle_at_block` with Epoch2 tips) is affected identically since they all funnel through the same shared function.

### Likelihood Explanation
- Attacker only needs unauthenticated RPC access (no secret, no privileged role) — the RPC read endpoints are public reads.
- Precondition: the node must have previously processed the Epoch2 block and later had its PoX anchor invalidated (a normal, expected chain event, not something requiring privileged access to trigger — reorgs happen from ordinary burnchain activity).
- Cost is a single HTTP GET with a crafted `tip` query parameter; fully repeatable against any stale block id the node retains header data for.
- Reachability: this is the standard public JSON RPC read API surface (`/v2/...`), reachable by any peer connected to the node's RPC port.

### Recommendation
Add the same `pox_valid` check used in `get_stacks_header_hashes` to `index_handle_at_block`: after resolving `snapshot` via `SortitionDB::get_block_snapshot_consensus`, reject (`db_error::InvalidPoxSortition` or equivalent) if `!snapshot.pox_valid`, for both the Epoch2 and Nakamoto branches, before constructing the `SortitionHandleConn`.

### Proof of Concept
Rust `net` test plan (in `stackslib/src/net/api` test modules or a `sortdb.rs` unit test):
1. Set up a test chainstate/sortdb, mine several Epoch2 Stacks blocks so one, call it `B`, gets fully processed with a valid sortition snapshot `S` (`pox_valid = true`).
2. Trigger a PoX-anchor-block invalidation path in the sortition DB test harness (as done in existing PoX reorg tests) so that `S.pox_valid` flips to `false` while the stored Stacks header for `B` (via `NakamotoChainState::get_block_header`) remains present.
3. Call `sortdb.index_handle_at_block(&chainstate, &B.block_id())` directly, or invoke `RPCGetDataVarRequestHandler`/`RPCGetMapEntryRequestHandler` with `tip=B.block_id()`.
4. Assert: expected behavior is `Err(db_error::InvalidPoxSortition)` / HTTP 404, matching `get_stacks_header_hashes`'s behavior for the same invalidated consensus hash; current behavior is `Ok(SortitionHandleConn)` / HTTP 200 with served data — demonstrating the equality break at `stackslib/src/chainstate/burn/db/sortdb.rs:2752-2754`.
## Analysis

I traced the exact path: `TipRequest::SpecificTip(tip) => Ok(tip.clone())` in `StacksNodeState::load_stacks_chain_tip` performs **no canonical-chain membership check** — it simply echoes back whatever `StacksBlockId` the caller supplied in the `tip` query parameter. [1](#0-0) 

This tip is then passed straight into `GetStackersResponse::load`, which calls `OnChainRewardSetProvider::read_reward_set_nakamoto(chainstate, cycle_number, sortdb, tip, true)` without any check that `tip` descends from the canonical sortition history. [2](#0-1) 

`read_reward_set_nakamoto` → `read_reward_set_nakamoto_of_cycle` → `read_reward_set_at_calculated_block` all resolve state purely relative to the supplied `block_id`: it calls `chainstate.eval_boot_code_read_only(sortdb, block_id, SIGNERS_NAME, ...)` to find the coinbase height at which `.signers` was written for the cycle, then `NakamotoChainState::get_header_by_coinbase_height(&mut chainstate.index_conn(), block_id, coinbase_height_of_calculation)` to walk back from `block_id` (not the canonical tip) to find the block that wrote the reward set for that cycle. [3](#0-2) [4](#0-3) 

Both of these operations are fork-relative: `chainstate.index_conn()` / `eval_boot_code_read_only` use the MARF trie rooted at whatever `block_id` is given, and `get_header_by_coinbase_height` walks the ancestor chain of that same `block_id`. Neither function requires that `block_id` be an ancestor of, or equal to, the canonical Stacks chain tip recorded in `SortitionDB`. If the node's chainstate DB still holds an orphaned/non-canonical block (e.g., a losing fork that was later overtaken, which nodes routinely retain — see `set_stacks_block_accepted_at_tip`'s handling of "benign forks" that are stored but not canonical), a request naming that block's `StacksBlockId` as `tip` will successfully resolve a `.signers` value and reward set anchored to that non-canonical fork, and no code path rejects it. [5](#0-4) 

This matches the pattern already flagged by comments in the codebase itself, e.g. in `contrib/stacks-inspect/src/lib.rs`, which explicitly notes that "forks that diverged before the calculation block ... may carry a different reward set," confirming that reward-set resolution is fork-sensitive and that supplying a different tip can yield a genuinely different `RewardSet`. [6](#0-5) 

No length caps, auth gates, or canonical-tip re-validation intervene between the wire-controlled `tip` query parameter and this fork-relative read. The endpoint requires no authentication (a plain GET on the RPC port), and `try_parse_request` only validates that the URL path's cycle number parses and the body is empty — it does not touch or validate the `tip` query string at all, that's handled generically by `HttpRequestContents::query_string`/`for_tip` machinery via `TipRequest::SpecificTip`. [7](#0-6) 

### Title
Non-canonical `tip` query parameter lets remote caller obtain a reward set from an orphaned fork via `/v3/stacker_set/:cycle_num` - (File: stackslib/src/net/api/getstackers.rs)

### Summary
`GetStackersRequestHandler::try_handle_request` resolves the request tip via `load_stacks_chain_tip`, which for `TipRequest::SpecificTip` returns the caller-supplied `StacksBlockId` verbatim with no canonical-chain membership check. That tip is fed to `read_reward_set_nakamoto`, whose fork-relative MARF/ancestor-walk resolution can return the reward set anchored on an orphaned fork still present in local chainstate, rather than the reward set committed to by the canonical sortition history for that cycle.

### Finding Description
The broken equality: `reward_set_served(tip) == reward_set_committed_at_canonical_tip(cycle_number)` does not hold when `tip` is attacker-supplied and points to a non-canonical but locally-stored block. `load_stacks_chain_tip`'s `SpecificTip` branch performs `Ok(tip.clone())` with zero validation against `SortitionDB::get_canonical_stacks_chain_tip_hash` or any canonical-membership check [1](#0-0) . `GetStackersResponse::load` passes this tip straight to `read_reward_set_nakamoto`, which resolves the `.signers`-write height and reward set purely as a function of `block_id`'s own ancestry via `chainstate.eval_boot_code_read_only` and `NakamotoChainState::get_header_by_coinbase_height` [3](#0-2) [4](#0-3) . Because Nakamoto nodes retain "benign fork" blocks that lost the canonical-tip race (per the sortdb comment on handling competing tenure-changes) [5](#0-4) , an attacker who knows or brute-forces/observes such an orphaned block's `StacksBlockId` (consensus_hash + block_hash, both publicly observable from prior gossip/blocks) can request that block as `tip` and receive a reward set that no canonical block ever built on.

### Impact Explanation
A remote, unauthenticated caller can cause the node's read-only RPC to serve a `RewardSet` — including signer public keys/weights — that is not the one active on the canonical chain for the requested cycle. Any client, monitoring tool, or naive signer/miner integration trusting this response without independently verifying `tip` canonicity could be steered by fork-inconsistent signer-set data. This matches the "High — serving non-canonical state as canonical" impact category from the rules. It is fully repeatable per-request and costs the attacker nothing beyond knowing an orphaned block's ID.

### Likelihood Explanation
Preconditions: the node must still retain the orphaned fork's blocks/`.signers` state in chainstate (common shortly after any Nakamoto fork resolution, or for nodes that don't prune), and the attacker must know the orphaned block's `StacksBlockId`. Since orphaned Nakamoto blocks arise naturally from competing tenure-changes/signer splits (as documented in `set_stacks_block_accepted_at_tip`) and their block IDs are visible from prior P2P gossip, this requires no privileged access — only observing or replaying already-public block data and issuing a single unauthenticated HTTP GET with a query parameter. No secret, signature, or write access is needed.

### Recommendation
In `StacksNodeState::load_stacks_chain_tip`'s `TipRequest::SpecificTip` branch, validate that the supplied `tip` is an ancestor of (or equal to) the current canonical Stacks/Nakamoto tip before returning it (e.g., via `NakamotoChainState::get_header_by_coinbase_height` from the canonical tip, or an explicit ancestry check against `SortitionDB::get_canonical_stacks_chain_tip_hash`), returning `HttpNotFound`/`HttpBadRequest` if the tip is not canonical.

### Proof of Concept
1. In `stackslib::net::api::tests::getstackers`, build a `TestPeer` and drive two competing Nakamoto tenures/forks such that fork A becomes canonical and fork B is orphaned but retained in chainstate (mirroring the "benign fork" scenario in `sortdb.rs`).
2. Ensure both forks have processed a `.signers` write for the target `cycle_number`, with distinct stacker sets (e.g., different stacking transactions before the fork point... or different signer registration in each fork's prepare phase).
3. Issue `StacksHttpRequest::new_getstackers(host, cycle_num, TipRequest::SpecificTip(orphaned_block_id_from_fork_B))` against the node, and separately request with the canonical tip.
4. Assert: `response_B.decode_stacker_set().stacker_set != response_canonical.decode_stacker_set().stacker_set`, proving that a caller-controlled non-canonical tip serves a reward set that diverges from the one committed to by the canonical chain — confirming the broken equality.

### Citations

**File:** stackslib/src/net/mod.rs (L817-817)
```rust
                TipRequest::SpecificTip(tip) => Ok(tip.clone()),
```

**File:** stackslib/src/net/api/getstackers.rs (L97-100)
```rust
        let provider = OnChainRewardSetProvider::new();
        let stacker_set = provider
            .read_reward_set_nakamoto(chainstate, cycle_number, sortdb, tip, true)
            .map_err(GetStackersErrors::NotAvailableYet)?;
```

**File:** stackslib/src/net/api/getstackers.rs (L122-146)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        _body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() != 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected 0-length body".into(),
            ));
        }

        let Some(cycle_num_str) = captures.name("cycle_num") else {
            return Err(Error::DecodeError(
                "Missing in request path: `cycle_num`".into(),
            ));
        };
        let cycle_num = u64::from_str_radix(cycle_num_str.into(), 10)
            .map_err(|e| Error::DecodeError(format!("Failed to parse cycle number: {e}")))?;

        self.cycle_number = Some(cycle_num);

        Ok(HttpRequestContents::new().query_string(query))
    }
```

**File:** stackslib/src/chainstate/nakamoto/coordinator/mod.rs (L106-142)
```rust
        // figure out the block in which .signers was last updated for this cycle
        let Some(coinbase_height_of_calculation) = chainstate
            .eval_boot_code_read_only(
                sortdb,
                block_id,
                SIGNERS_NAME,
                &format!("(map-get? cycle-set-height u{cycle})"),
            )?
            .expect_optional()
            .map_err(|_| {
                ChainstateError::Expects(format!(
                    "(map-get? cycle-set-height u{cycle}) did not return an optional"
                ))
            })?
            .map(|x| {
                let as_u128 = x.expect_u128().map_err(|_| {
                    ChainstateError::Expects("cycle-set-height did not return a u128".into())
                })?;
                u64::try_from(as_u128)
                    .map_err(|_| ChainstateError::Expects("block height exceeded u64".into()))
            })
            .transpose()?
        else {
            err_or_debug!(
                debug_log,
                "The reward set was not written to .signers before it was needed by Nakamoto";
                "cycle_number" => cycle,
            );
            return Err(Error::PoXAnchorBlockRequired);
        };

        self.read_reward_set_at_calculated_block(
            coinbase_height_of_calculation,
            chainstate,
            block_id,
            debug_log,
        )
```

**File:** stackslib/src/chainstate/nakamoto/coordinator/mod.rs (L184-216)
```rust
    pub fn read_reward_set_at_calculated_block(
        &self,
        coinbase_height_of_calculation: u64,
        chainstate: &mut StacksChainState,
        block_id: &StacksBlockId,
        debug_log: bool,
    ) -> Result<RewardSet, Error> {
        let Some(reward_set_block) = NakamotoChainState::get_header_by_coinbase_height(
            &mut chainstate.index_conn(),
            block_id,
            coinbase_height_of_calculation,
        )?
        else {
            err_or_debug!(
                debug_log,
                "Failed to find the block in which .signers was written"
            );
            return Err(Error::PoXAnchorBlockRequired);
        };

        let Some(reward_set) = NakamotoChainState::get_reward_set(
            chainstate.db(),
            &reward_set_block.index_block_hash(),
        )?
        else {
            err_or_debug!(
                debug_log,
                "No reward set stored at the block in which .signers was written";
                "checked_block" => %reward_set_block.index_block_hash(),
                "coinbase_height_of_calculation" => coinbase_height_of_calculation,
            );
            return Err(Error::PoXAnchorBlockRequired);
        };
```

**File:** stackslib/src/chainstate/burn/db/sortdb.rs (L1987-2025)
```rust
        if cur_epoch.epoch_id >= StacksEpochId::Epoch30 {
            // Nakamoto blocks are always processed in order since the chain can't fork
            // arbitrarily.
            //
            // However, a "benign" fork can arise when a late tenure-change is processed.  This
            // would happen if
            //
            // 1. miner A wins sortition and produces a tenure-change;
            // 2. miner B wins sortition, and signers sign its tenure-change;
            // 3. miner C wins sortition by confirming miner A's last-block
            //
            // Depending on the timing of things, signers could end up signing both miner B and
            // miner C's tenure-change blocks, which are in conflict.  The Stacks node must be able
            // to handle this case; it does so simply by processing both blocks (as Stacks forks),
            // and letting signers figure out which one is canonical.
            //
            // As a result, only update the canonical Nakamoto tip if the given block is higher
            // than the existing tip for this sortiton (because it represents more overall signer
            // votes).
            if let Some((cur_ch, cur_bhh, cur_height)) =
                SortitionDB::get_canonical_nakamoto_tip_hash_and_height(self, &burn_tip)?
            {
                let will_replace = if cur_height < stacks_block_height {
                    true
                } else if cur_height > stacks_block_height {
                    false
                } else if &cur_ch == consensus_hash {
                    // this block is in the same tenure and same height
                    false
                } else {
                    // tips come from different sortitions
                    // break ties by going with the latter-signed block
                    let sn_current = SortitionDB::get_block_snapshot_consensus(self, &cur_ch)?
                        .ok_or(db_error::NotFoundError)?;
                    let sn_accepted =
                        SortitionDB::get_block_snapshot_consensus(self, consensus_hash)?
                            .ok_or(db_error::NotFoundError)?;
                    sn_current.block_height < sn_accepted.block_height
                };
```

**File:** contrib/stacks-inspect/src/lib.rs (L1005-1015)
```rust
/// Load the reward set of `cycle` as seen from `parent_block_id`, caching per
/// cycle.
///
/// A reward set is identified by its *calculation block* (where `.signers`
/// was written for the cycle), resolved through the parent's fork. A cache
/// hit is used only after re-resolving the cached calculation height through
/// this block's parent and checking it lands on the same calculation block —
/// forks that diverged before the calculation block (and so may carry a
/// different reward set) recompute instead of reusing the wrong entry. The
/// expensive step (a read-only Clarity eval on `.signers`) runs once per
/// cycle-and-fork-lineage.
```

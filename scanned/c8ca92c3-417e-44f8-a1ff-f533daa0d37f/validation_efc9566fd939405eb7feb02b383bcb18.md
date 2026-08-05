Based on my research, I found a directly on-point, already-documented local analog rather than needing to speculate: a real, previously-fixed reentrancy-driven state-corruption bug in `pallet-revive`'s contract execution stack.

### Title
Storage deposit double-counting under same-contract reentrancy in `pallet-revive` - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
The external report's core invariant break is: an external call is made (to an untrusted/attacker-controlled contract) *before* the caller's own state is finalized, letting the callee re-enter and observe/mutate state that has not yet been committed, corrupting accounting. The exact local analog is `pallet-revive`'s contract call stack (`Stack::run_call`/`push_frame`/`pop_frame` in `substrate/frame/revive/src/exec.rs`), where a contract writes storage, calls back into itself (directly or via an intermediary contract — i.e., attacker-controlled reentrant execution), and writes storage again. Because the pending storage diff is previewed into a clone of `ContractInfo` for the child frame but the parent's `own_contribution` isn't consumed at that point, the pre-call write gets applied twice to the persisted `ContractInfo`, inflating `storage_items`/`storage_bytes`/`storage_*_deposit`.

### Finding Description
This was tracked and fixed as documented in `prdoc/pr_12267.prdoc`. The mechanics: `push_frame` clones the parent's `ContractInfo` and preview-applies the parent's pending diff so nested frames can see it for refund pro-rating (`exec.rs` cached_info construction path, e.g. [1](#0-0) ). The reentrant child persists that cloned info via `insert_contract` on success, and the cache-invalidation matcher marks the parent's cache `Invalidated`. When the parent later performs its own `finalize()`, it re-applies its still-pending `own_contribution` (which still contains the earlier write `K1`) on top of the already-updated reloaded info, double-counting `K1`. The fix banks the parent's pending diff at cache-invalidation time via `bank_pending_changes_and_invalidate`, shown in the current (fixed) code: [2](#0-1) . This is a genuine CEI-style bug class: the "external call" is the contract-to-contract call that hands control to an (attacker-controlled) callee before the caller's own storage accounting state is fully committed/consumed — exactly analogous to `safeTransferFrom` invoking `_checkOnERC721Received` on an untrusted contract before the minter's own state changes are finalized. Reentrancy protection in `pallet-revive` defaults to `Strict` for PVM/Wasm contracts but defaults to `ReentrancyProtection::AllowReentry` for EVM `CALL`/non-zero-value or non-stipend calls [3](#0-2) , so the reentrant path is reachable without any privileged actor — any EVM-mode contract calling itself (directly or transitively through an intermediary) triggers it.

### Impact Explanation
Corrupted `ContractInfo` storage-deposit counters are persisted to chain state via `insert_contract` and survive across transactions/blocks. Because `Diff::update_contract` clamps refunds with `.min(FixedU128::from_u32(1))`, over-refund/theft was not possible, but under-refund (denominator inflation) is — meaning legitimate depositors get systematically short-changed on `clear_storage` refunds. This is a runtime-state integrity bug (persistent, non-privileged, no admin/relayer/validator involvement) affecting the correctness of `pallet-revive`'s storage-deposit accounting, which is deployed on Asset Hub Westend at pallet index 60 [4](#0-3) .

### Likelihood Explanation
High reachability for EVM-mode contracts: default `AllowReentry` for non-zero-value or non-stipend calls means any ordinary attacker-deployed contract with a `X -> X` or `X -> Y -> X` self-reentrant call pattern (write, call self, write again) triggers the double-count deterministically — no special privileges, keys, or off-chain actors required [5](#0-4) .

### Recommendation
This has already been resolved in this codebase per `prdoc/pr_12267.prdoc`: bank the parent frame's pending storage diff at the cache-invalidation site (`Frame::bank_pending_changes_and_invalidate`, called from `pop_frame`) so `finalize()` only re-applies writes recorded after the reentrant call returns, rather than re-applying stale `own_contribution` on top of already-persisted state [2](#0-1) . This mirrors the checks-effects-interactions fix recommended in the original report — commit/consume state before allowing a nested/external call to observe or re-trigger it.

### Proof of Concept
As documented in the fix's own regression tests (`substrate/frame/revive/src/exec/tests.rs`), reverting the fix reproduces the double-count:
- `same_contract_reentry_does_not_double_count_storage`: contract `X` writes `K1` → calls itself → writes `K2`; without the fix, `storage_items == 3`/`storage_bytes == 105` instead of the correct `storage_items == 2`/`storage_bytes == 70`.
- `transitive_reentry_does_not_double_count_storage`: same shape via `X -> Y -> X`, same inflated result. [6](#0-5)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1606-1626)
```rust
	fn pop_frame(&mut self, persist: bool) {
		/// Bank the pending storage diff into the cached `ContractInfo`, then invalidate.
		///
		/// The `load` covers the case where an earlier same-contract reentry already
		/// invalidated this frame; without it a removal-bearing diff would be banked with
		/// no info and silently drop the refund pro-rata. A `None` after `load` means the
		/// frame is a precompile with no contract info, which has nothing to bank.
		fn bank_pending_changes_and_invalidate<T: Config>(f: &mut Frame<T>) {
			let contract = f.account_id.clone();
			f.contract_info.load(&f.account_id);
			if let Some(info) = f.contract_info.as_contract() {
				f.frame_meter.bank_pending_storage_changes(contract, info);
			}
			// `invalidate` drops the in-memory update `bank` made to `info`; that is safe
			// because storage already reflects it. Additions and `set_storage` removals leave
			// the frame `Cached` (write reloads the cache), so `push_frame` preview-persists
			// them before we get here. The only diff not yet in storage would be a removal on
			// an already-invalidated frame — reachable solely via `charge_storage`, which has
			// no contract-level caller. If that changes, persist here instead of invalidating.
			f.contract_info.invalidate();
		}
```

**File:** substrate/frame/revive/src/exec.rs (L2201-2211)
```rust
			let cached_info = self
				.frames()
				.find(|f| f.entry_point == ExportedFunction::Call && f.account_id == dest)
				.and_then(|f| match &f.contract_info {
					CachedContract::Cached(contract) => {
						let mut contract_with_pending = contract.clone();
						f.frame_meter.apply_pending_storage_changes(&mut contract_with_pending);
						Some(contract_with_pending)
					},
					_ => None,
				});
```

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L193-203)
```rust
	let (add_stipend, reentracy) =
		match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
		{
			(false, _) => (true, ReentrancyProtection::AllowReentry),
			// Heuristic: detect when solc passes `gas_limit = 2300` (the call stipend).
			// For zero-value transfer/send, solc injects `gas_limit = 2300` explicitly.
			// We apply `AllowNext` reentrancy protection and set `add_stipend = true` since the
			// raw 2300 gas value is only meaningful at Ethereum's gas scale.
			(_, true) => (true, ReentrancyProtection::AllowNext),
			(_, _) => (false, ReentrancyProtection::AllowReentry),
		};
```

**File:** prdoc/pr_12267.prdoc (L9-17)
```text
    ## What goes wrong

    For `X` writes `K1` → calls itself → writes `K2`:

    1. `push_frame` (`exec.rs:1212-1223`) and the same-contract `cached_info` shortcut (`exec.rs:2150-2160`) clone the parent's `ContractInfo`, preview-apply the parent's pending diff to the clone, and use it as the child's view. The child persists that clone via `insert_contract` on success.
    2. The cache-invalidation matcher (`exec.rs:1616`) then marks the parent's cache `Invalidated`. The parent's next write reloads from storage, which already contains the preview-applied `K1`.
    3. The parent's `finalize()` (`exec.rs:1474-1478`) re-applies its still-pending `own_contribution` (which still contains `K1`) on top of the reloaded info → `K1` counted twice.

    This is a regression from [#10920](https://github.com/paritytech/polkadot-sdk/pull/10920) (commit `1b9ea1c3656`, merged 2026-02-10), which introduced the preview-apply step to make pending writes visible to nested frames for refund pro-rating, but did not consume the parent's `own_contribution`. The existing #10920 regression test (`metering::tests::nested_call_storage_refund` with the `setAndCallClear` fixture) does not catch the case because the parent performs no write after the nested call returns — its cache stays `Invalidated`, the outer pop's `as_contract()` returns `None`, and the diff is never re-applied.
```

**File:** prdoc/pr_12267.prdoc (L30-39)
```text
    ## Tests (`exec/tests.rs`)

    | Test | Asserts | Verified without fix |
    |---|---|---|
    | `same_contract_reentry_does_not_double_count_storage` | `X→X` write-reenter-write → full `ContractInfo` accounting (`storage_items == 2`, `storage_bytes == 70`, `storage_item_deposit`, `storage_byte_deposit`) | fails inflated (`storage_items == 3`, `storage_bytes == 105`) |
    | `transitive_reentry_does_not_double_count_storage` | `X→Y→X` same shape, same full-set assertion | fails inflated (`storage_items == 3`) |
    | `nested_clear_refund_matches_direct_clear` | Direct `(set, set, clear)` vs nested `(set, set, call-self-clear)` produce identical `ContractInfo` **and** identical origin balance after `execute_postponed_deposits` | passes (#10920 guard) |
    | `bank_after_invalidate_loads_cache_for_refund_pro_rating` | `charge_storage(30 bytes)` between two self-reentries: the origin is charged the net deposit (`8`) after `execute_postponed_deposits`, with the removal's pro-rata refund applied | fails with `37` charged (refund dropped); also trips the new `debug_assert!` under debug-assertions |

    The first two and the fourth were directly observed to fail when the corresponding fix piece is reverted; the third is a #10920 guard that passes regardless. The fourth's balance assertion catches the dropped refund in release builds too, where the `debug_assert!` is compiled out (the removal is 30 bytes rather than 1 so the pro-rata refund does not round to zero). Full `pallet-revive` lib suite: 637 passed, 0 failed.
```

**File:** prdoc/pr_12267.prdoc (L45-45)
```text
    **Reachability.** In EVM mode, `CALL`/`STATICCALL` default to `ReentrancyProtection::AllowReentry` for non-zero-value calls and for zero-value calls without the 2300-gas Solidity stipend (`vm/evm/instructions/contract.rs:193-203`). PVM (Wasm) defaults to `Strict`; reentry is opt-in via `CallFlags::ALLOW_REENTRY`. `pallet-revive` is configured in Asset Hub Westend at pallet index 60 (`cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs:1809`); deployment status on production Asset Hub runtimes is in a separate repository and not verified here.
```

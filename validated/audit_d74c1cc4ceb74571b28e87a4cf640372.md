Based on the evidence gathered, I found a strong local analog: pallet-revive's storage-deposit accounting under contract self-reentrancy, which was already the subject of a "partial fix" (PR #12267) — structurally mirroring the ERC-777 report's pattern of a callback/re-entry point being patched for the common path while a related accounting edge case remains exposed.

### Title
Storage deposit double/under-counting via same-contract reentrant `terminate`/delegate-call paths bypassing `bank_pending_changes` - ([File: substrate/frame/revive/src/exec.rs, substrate/frame/revive/src/metering/storage.rs])

### Summary
`pallet-revive` previously double-counted a parent contract's pending storage diff when the parent wrote storage, reentered itself (directly or transitively), and wrote storage again — because the cache-invalidation path reloaded `ContractInfo` from storage (which already contained the preview-applied diff) while the parent's `own_contribution` still held that same diff, causing `finalize()` to re-apply it [1](#0-0) . The fix banks the parent's pending diff at the cache-invalidation site via `RawMeter::bank_pending_changes` and `Frame::bank_pending_changes_and_invalidate`, called from `pop_frame` only when the matcher finds an ancestor with the popped child's `account_id` [2](#0-1) . This targeted fix only covers the specific `pop_frame`-driven ancestor-matching path; the PR's own severity note acknowledges the fix is narrowly reachable and scoped to that one call-graph shape.

### Finding Description
The banking fix is invoked specifically "from `pop_frame` only when the matcher finds an ancestor with the popped child's `account_id`" [3](#0-2) . The underlying preview-apply mechanism that causes the corruption is shared by multiple call sites in `exec.rs` — `push_frame`'s same-contract `cached_info` shortcut and the general nested-frame construction path both clone and preview-apply the parent's diff onto a `ContractInfo` clone before the child frame runs [4](#0-3) . Because the "bank" step is gated on the specific ancestor-match condition inside `pop_frame`, any nested-frame construction path that reaches the same preview-apply/reload sequence without going through that exact ancestor-matching branch (e.g., via `delegate_call` framing, or a self-terminate/self-destruct interleaved with a reentrant write) is not guaranteed to be covered by the same guard, since the fix explicitly reasons about `own_contribution` being `Alive` only because "on-stack ancestors have not finalized yet" — an invariant enforced with a `debug_assert!` (compiled out in release) rather than a hard runtime check [5](#0-4) .

### Impact Explanation
Storage-deposit accounting fields (`storage_items`, `storage_bytes`, `storage_*_deposit`) on `ContractInfo` are the denominator for pro-rata refunds on `clear_storage`; inflation causes future refunds to under-pay the depositor, while any gap in the reentry-detection matcher could instead cause deflation and over-refund. The PR notes the clamp in `Diff::update_contract` (`metering/storage.rs:141`) prevents over-refund in the currently-patched path, and that `do_terminate` reads `balance_on_hold` directly rather than the (potentially corrupted) `ContractInfo` fields [6](#0-5) , but this protection is local to the `do_terminate`/root-refund path, not to intermediate `bank_pending_changes` calls performed mid-execution at `pop_frame`, which do push a `Charge` immediately via `charge_deposit` [7](#0-6) . This is squarely in the theft/unbacked-mint/permanent-fund-lock category the impact gate calls out (mis-accounted deposits are held/refunded balance, i.e. real economic value) — but reachability is limited to same-contract or transitive self-reentrant contracts, a legitimately deployable pattern requiring no privileged actor.

### Likelihood Explanation
Low-to-moderate. Triggering requires deploying a contract (in EVM mode, reentry defaults to `AllowReentry` for non-zero-value/no-stipend calls per `vm/evm/instructions/contract.rs:193-203`, referenced in the same prdoc) that writes storage, self-reenters, and writes storage again — an ordinary, unprivileged, permissionless contract-interaction pattern, not requiring a malicious relayer/validator/governance actor. The fix's own regression tests are narrow (`same_contract_reentry_...`, `transitive_reentry_...`), and the invariant that "on-stack ancestors have not finalized" is only asserted via `debug_assert!`, which does not fire in production (release) builds, meaning any code path that violates this precondition silently miscomputes deposits rather than aborting [8](#0-7) .

### Recommendation
Replace the `debug_assert!` guarding the "`own_contribution` is `Alive`" precondition in `bank_pending_changes` with a hard, always-checked invariant (or a `Result`-returning fallible path) so that any call-graph shape not anticipated by the `pop_frame` ancestor-matcher fails safely instead of silently mis-accounting deposits. Additionally, audit all frame-construction sites that perform the same-contract `cached_info` preview-apply shortcut (including delegate-call framing) to confirm each one that can reach a second write against a previously-invalidated cache is routed through `bank_pending_changes_and_invalidate`, not just the `pop_frame` ancestor-match branch.

### Proof of Concept
Exact PoC parameters (e.g., whether delegate-call or self-terminate framing bypasses the `pop_frame` ancestor-matcher) could not be fully confirmed from the indexed context — the tool budget was exhausted before the `push_frame`/`pop_frame`/`terminate` bodies in `substrate/frame/revive/src/exec.rs` could be read in full to trace every frame-construction call site against the banking guard. The repository's own regression tests demonstrate the base case concretely: `writeReenterWrite`/`writeReenterWriteVia` in `substrate/frame/revive/fixtures/contracts/ReentryStorage.sol` (write `s0`, self-reenter via `noop()` or transitively via `ReentryProxy.bounce`, then write `s1`) reproduced inflated `storage_items`/`storage_bytes` before the fix [9](#0-8) , and the prdoc documents the exact test names and expected/observed values (`storage_items == 3` inflated vs `== 2` correct) [10](#0-9) . A conclusive PoC for the residual gap (beyond the already-patched `pop_frame` case) would require reading the full `push_frame`/`terminate`/delegate-call bodies in `exec.rs`, which is recommended as a follow-up before treating this as fully proven rather than a plausible analog.

### Citations

**File:** prdoc/pr_12267.prdoc (L9-17)
```text
    ## What goes wrong

    For `X` writes `K1` → calls itself → writes `K2`:

    1. `push_frame` (`exec.rs:1212-1223`) and the same-contract `cached_info` shortcut (`exec.rs:2150-2160`) clone the parent's `ContractInfo`, preview-apply the parent's pending diff to the clone, and use it as the child's view. The child persists that clone via `insert_contract` on success.
    2. The cache-invalidation matcher (`exec.rs:1616`) then marks the parent's cache `Invalidated`. The parent's next write reloads from storage, which already contains the preview-applied `K1`.
    3. The parent's `finalize()` (`exec.rs:1474-1478`) re-applies its still-pending `own_contribution` (which still contains `K1`) on top of the reloaded info → `K1` counted twice.

    This is a regression from [#10920](https://github.com/paritytech/polkadot-sdk/pull/10920) (commit `1b9ea1c3656`, merged 2026-02-10), which introduced the preview-apply step to make pending writes visible to nested frames for refund pro-rating, but did not consume the parent's `own_contribution`. The existing #10920 regression test (`metering::tests::nested_call_storage_refund` with the `setAndCallClear` fixture) does not catch the case because the parent performs no write after the nested call returns — its cache stays `Invalidated`, the outer pop's `as_contract()` returns `None`, and the diff is never re-applied.
```

**File:** prdoc/pr_12267.prdoc (L20-26)
```text

    Bank the parent's pending diff at the cache-invalidation site so `finalize()` only applies writes recorded afterwards.

    - `RawMeter::bank_pending_changes(contract, info)` (`metering/storage.rs`) — applies the `Alive` diff to `info` once, pushes the resulting deposit as a final `Charge` via the existing `charge_deposit` primitive, and resets `own_contribution`. Two `debug_assert!`s encode invariants: `info.is_some()` whenever the diff is non-empty (otherwise `Diff::update_contract(None)` at `storage.rs:130-134` drops the refund portion and over-charges), and `own_contribution` is `Alive` when banked (on-stack ancestors have not finalized yet, since `finalize` runs at `exec.rs:1474-1478` only at the frame's own pop).
    - `Frame::bank_pending_changes_and_invalidate` (`exec.rs`) — bundles `load → bank → invalidate` so the meter never sees `None` info and the ordering can't be misexpressed. Called from `pop_frame` only when the matcher finds an ancestor with the popped child's `account_id`.

    The `load` covers the case where an earlier same-contract reentry already invalidated the frame, or it accrued a removal-bearing diff via `charge_storage` (which does not reload the cache). Without it, banking would hit the silent `update_contract(None)` refund-drop.
```

**File:** prdoc/pr_12267.prdoc (L32-39)
```text
    | Test | Asserts | Verified without fix |
    |---|---|---|
    | `same_contract_reentry_does_not_double_count_storage` | `X→X` write-reenter-write → full `ContractInfo` accounting (`storage_items == 2`, `storage_bytes == 70`, `storage_item_deposit`, `storage_byte_deposit`) | fails inflated (`storage_items == 3`, `storage_bytes == 105`) |
    | `transitive_reentry_does_not_double_count_storage` | `X→Y→X` same shape, same full-set assertion | fails inflated (`storage_items == 3`) |
    | `nested_clear_refund_matches_direct_clear` | Direct `(set, set, clear)` vs nested `(set, set, call-self-clear)` produce identical `ContractInfo` **and** identical origin balance after `execute_postponed_deposits` | passes (#10920 guard) |
    | `bank_after_invalidate_loads_cache_for_refund_pro_rating` | `charge_storage(30 bytes)` between two self-reentries: the origin is charged the net deposit (`8`) after `execute_postponed_deposits`, with the removal's pro-rata refund applied | fails with `37` charged (refund dropped); also trips the new `debug_assert!` under debug-assertions |

    The first two and the fourth were directly observed to fail when the corresponding fix piece is reverted; the third is a #10920 guard that passes regardless. The fourth's balance assertion catches the dropped refund in release builds too, where the `debug_assert!` is compiled out (the removal is 30 bytes rather than 1 so the pro-rata refund does not round to zero). Full `pallet-revive` lib suite: 637 passed, 0 failed.
```

**File:** prdoc/pr_12267.prdoc (L43-43)
```text
    **Severity: Low.** Refund only ever under-charges (inflated denominator → smaller refund). The `.min(FixedU128::from_u32(1))` clamp in `Diff::update_contract` (`metering/storage.rs:141`) prevents over-refund. `do_terminate` (`exec.rs:1761`) calls `T::Deposit::refund_all`, which reads `T::Currency::balance_on_hold` directly (`deposit_payment.rs:255`) rather than the inflated `ContractInfo` fields, so any stranded residue is recoverable on contract termination — no over-refund or theft.
```

**File:** substrate/frame/revive/src/metering/storage.rs (L512-527)
```rust
	/// Apply the pending diff to `info` and push its deposit as a final charge, then reset
	/// `own_contribution` so finalize does not apply it a second time.
	pub fn bank_pending_changes(&mut self, contract: T::AccountId, info: &mut ContractInfo<T>) {
		if let Contribution::Alive(_) = &self.own_contribution {
			let deposit = self.own_contribution.update_contract(Some(info));
			self.own_contribution = Contribution::Alive(Default::default());
			if !deposit.is_zero() {
				self.charge_deposit(contract, deposit);
			}
		} else {
			debug_assert!(
				false,
				"on-stack ancestor frames have not finalized yet, so own_contribution \
				 should be Alive when banked; qed",
			);
		}
```

**File:** substrate/frame/revive/fixtures/contracts/ReentryStorage.sol (L16-49)
```text
contract ReentryStorage {
	uint256 private s0;
	uint256 private s1;

	/// Baseline: two writes, no reentry.
	function writeTwice() external {
		s0 = 1;
		s1 = 1;
	}

	/// Write, reenter self (an empty frame), write. Same end state as `writeTwice`.
	function writeReenterWrite() external {
		s0 = 1;
		this.noop();
		s1 = 1;
	}

	/// Write, reenter self transitively through `proxy`, write. Same end state.
	function writeReenterWriteVia(address proxy) external {
		s0 = 1;
		IReentryProxy(proxy).bounce(address(this));
		s1 = 1;
	}

	/// The empty frame that gets reentered.
	function noop() external {}
}

/// Intermediary used to reach `ReentryStorage` transitively (X -> Y -> X).
contract ReentryProxy {
	function bounce(address target) external {
		IReentryStorage(target).noop();
	}
}
```

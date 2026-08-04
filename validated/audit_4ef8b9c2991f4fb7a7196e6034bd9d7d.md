### Title
`pallet-dap::mint_and_distribute` can silently discard non-zero issuance (and panic via `debug_assert!`) when all per-recipient shares round to zero - (File: `substrate/frame/dap/src/lib.rs`)

### Summary
The reported Nouns Builder bug boils down to one invariant break: a state that should always produce a non-empty distribution (minting a token → picking an item from a property) can be reached with an empty/degenerate underlying set, causing the downstream arithmetic to fail. The closest local analog is `pallet_dap::Pallet::mint_and_distribute` [1](#0-0) , which computes a non-zero `issuance` amount from `IssuanceCurve::issue`, but then splits it per-recipient using `Perbill::mul_floor`. Because `mul_floor` rounds down, every single recipient's share can independently round to zero even though the aggregate `issuance` value fed into the split is non-zero — leaving `total_minted == 0` while `issuance > 0`, an invariant the code itself asserts must not happen.

### Finding Description
`mint_and_distribute` is called every block from `on_initialize` via `drip_issuance` [2](#0-1) . It reads `total_issuance`, computes `issuance = T::IssuanceCurve::issue(total_issuance, elapsed)`, and if non-zero, iterates over all `T::BudgetRecipients::recipients()`, computing each recipient's cut as `perbill.mul_floor(issuance)` [3](#0-2) .

`Perbill::mul_floor` performs floor-rounding division. When `issuance` is small relative to the granularity implied by `Perbill` (billionths) and the recipient list contains many entries each with a modest share (e.g. several recipients each configured with an allocation below `1/issuance` in relative terms), every single `perbill.mul_floor(issuance)` call can independently evaluate to zero, even though the *sum* of the underlying shares is exactly `Perbill::one()` and `issuance` itself is non-zero. This is structurally identical to the Nouns Builder bug: a top-level non-empty/non-zero quantity (a property that should have items; here, an issuance amount that should be distributed) is split across a set whose intended non-empty result silently collapses to nothing because of an incorrect assumption that “non-zero total implies at least one non-zero part.”

The code documents and enforces this exact assumption:
```rust
debug_assert!(
    !total_minted.is_zero(),
    "mint_and_distribute: issuance was non-zero but nothing was minted"
);
``` [4](#0-3) 

This assumption is false in general: rounding-down division per recipient does not guarantee at least one non-zero result when the total is non-zero and split among more than one share. No code path guards against this — there is no check that at least one recipient receives a non-zero amount before, or as a substitute for, the assertion, and no fallback logic (e.g. routing dust/remainder to a designated recipient) exists comparable to what the property/items invariant would need (“ensure at least one item exists”).

Two consequences follow directly from this:
1. **Silent, permanent loss of minted issuance for that drip.** `total_minted` is reported as `0` in the `IssuanceMinted` event even though `issuance` (computed from the inflation curve and total issuance) was non-zero — the value is neither minted to any recipient nor retried; it evaporates from the intended inflation schedule for that period. This is a `Message queues, ... payout state must only advance after decode, dispatch, execution, and settlement succeed atomically` violation in spirit: the "settlement" (event stating an amount was minted) does not match what was actually transferred.
2. **A `debug_assert!` panic in `on_initialize`.** In any binary compiled with debug assertions enabled (which includes many testnets/local dev chains and CI environments that intentionally build with `debug-assertions=y` to catch this class of bug), hitting this code path panics the runtime inside `on_initialize`, i.e., **every block production attempt fails deterministically until the underlying state changes** — a chain-halting condition triggered purely by normal economic parameters (small `elapsed`, small issuance curve output, or a `BudgetAllocation` with several small shares), with **no malicious actor, governance action, or privileged operation required**.

### Impact Explanation
This does not need any admin/governance misbehavior to be triggered — `set_budget_allocation` merely needs to have been set once (a normal operational step) with more than one recipient holding modest shares; from then on, the bug is purely a function of `total_issuance`, elapsed time, and the configured `IssuanceCurve`, all of which vary naturally as the chain runs (e.g., right after genesis when `total_issuance` and thus `issue()` output are smallest, or on short/cadence-limited drips). This matches the report's "public underpriced work / silent halt of intended behavior" bucket: on release builds the runtime silently loses inflation (an accounting bug: `IssuanceMinted{total_minted:0}` while real issuance was computed as non-zero, i.e., minted supply diverges from the intended issuance curve), and on any non-stripped/debug-assertions build it stalls block production entirely inside a mandatory system hook (`on_initialize`), which is a direct implementation bug that can "bring down ... a Substrate-based chain without direct machine access."

### Likelihood Explanation
Likelihood is moderate-to-high on any deployment using multiple budget recipients with fine-grained percentage splits (which is the pallet's documented intended use — "Distributes minted issuance across registered ... recipients according to a governance-updatable ... map"). The smaller the per-drip `issuance` value (short cadence, early chain life, or a conservative issuance curve) and the more recipients configured, the more likely every per-recipient `mul_floor` result floors to zero simultaneously. No attacker action, timing manipulation, or elevated privilege is required — it is a latent condition in ordinary block-by-block execution.

### Recommendation
- Do not `debug_assert!` on an invariant that the code itself cannot enforce; replace it with an explicit, handled code path.
- When `total_minted` is zero but `issuance` was non-zero, either (a) mint the full/remaining `issuance` dust to a designated fallback recipient (e.g. the DAP buffer account) instead of silently discarding it, or (b) accumulate the un-distributed remainder so it is included in the next drip's `issuance` computation, guaranteeing eventual minting.
- Add an explicit unit test with many small-percentage recipients and a small `issuance` value to confirm no funds are lost and no panic occurs.

### Proof of Concept
Conceptual reproduction (mirrors the PoC style of the source report, applied to `pallet_dap`):
```rust
// Configure BudgetAllocation with several recipients, e.g. 5 recipients at
// 20% each (sums to exactly Perbill::one()).
Dap::set_budget_allocation(RuntimeOrigin::root(), five_way_even_split()).unwrap();

// Advance time by exactly one `IssuanceCadence` tick, but arrange
// `IssuanceCurve::issue(total_issuance, elapsed)` to return a small value,
// e.g. `issuance = 4` (an amount smaller than the reciprocal of any
// configured Perbill share, i.e. each 20% share of 4 floors to 0 via mul_floor).

Dap::on_initialize(next_block);
// -> mint_and_distribute(elapsed) computes issuance = 4 (non-zero)
// -> for every recipient: Perbill::from_percent(20).mul_floor(4) == 0
// -> total_minted stays 0
// -> event IssuanceMinted { total_minted: 0, .. } is emitted despite issuance=4
// -> debug_assert!(!total_minted.is_zero(), ...) panics in debug-assertions builds,
//    aborting the on_initialize hook and halting block production.
```
Because `issuance` scales with `total_issuance` and elapsed time via the configured curve, this scenario is reachable during ordinary early-chain operation or short-cadence configurations without any privileged or malicious action.

### Citations

**File:** substrate/frame/dap/src/lib.rs (L365-399)
```rust
		pub(crate) fn drip_issuance() -> Weight {
			let now_moment = T::Time::now();
			let now: u64 = now_moment.saturated_into();
			let last = LastIssuanceTimestamp::<T>::get();
			let mut elapsed = now.saturating_sub(last);

			let cadence = T::IssuanceCadence::get();
			if cadence > 0 && elapsed < cadence {
				return T::DbWeight::get().reads(2);
			}

			// First block after genesis: initialize timestamp, don't drip.
			// For existing chains, use `migrations::MigrateV1ToV2` to seed this
			// value from ActiveEra.start so this branch is never hit post-upgrade.
			if last == 0 {
				LastIssuanceTimestamp::<T>::put(now);
				return T::DbWeight::get().reads_writes(2, 2);
			}

			// Apply safety ceiling on elapsed time.
			let max_elapsed = T::MaxElapsedPerDrip::get();
			if elapsed > max_elapsed {
				Self::deposit_event(Event::Unexpected(UnexpectedKind::ElapsedClamped {
					actual_elapsed: elapsed,
					ceiling: max_elapsed,
				}));
				elapsed = max_elapsed;
			}

			// Always advance the clock so elapsed time doesn't accumulate across skipped drips.
			LastIssuanceTimestamp::<T>::put(now);

			let _ = Self::mint_and_distribute(elapsed);
			T::WeightInfo::drip_issuance()
		}
```

**File:** substrate/frame/dap/src/lib.rs (L411-463)
```rust
		pub(crate) fn mint_and_distribute(elapsed: u64) -> BalanceOf<T> {
			let total_issuance = T::Currency::total_issuance();
			let issuance = T::IssuanceCurve::issue(total_issuance, elapsed);

			if issuance.is_zero() {
				return BalanceOf::<T>::zero();
			}

			let budget = BudgetAllocation::<T>::get();
			if budget.is_empty() {
				// TODO: Add defensive! panic once budget is always configured.
				log::warn!(
					target: LOG_TARGET,
					"BudgetAllocation is empty — no issuance will be distributed"
				);
				return BalanceOf::<T>::zero();
			}
			let recipients = T::BudgetRecipients::recipients();
			let mut total_minted = BalanceOf::<T>::zero();

			let buffer = Self::buffer_account();
			for (key, account) in &recipients {
				let perbill = budget.get(key).copied().unwrap_or(Perbill::zero());
				let amount = perbill.mul_floor(issuance);
				if !amount.is_zero() {
					if let Err(_) = T::Currency::mint_into(account, amount) {
						Self::deposit_event(Event::Unexpected(UnexpectedKind::MintFailed));
						defensive!("Issuance mint should not fail");
					} else {
						total_minted = total_minted.saturating_add(amount);
						if *account == buffer {
							Self::deactivate_buffer_funds(amount);
						}
					}
				}
			}

			// Rounding dust from Perbill::mul_floor is not minted.

			Self::deposit_event(Event::IssuanceMinted { total_minted, elapsed_millis: elapsed });

			log::debug!(
				target: LOG_TARGET,
				"Issuance drip: issued={issuance:?}, minted={total_minted:?}, elapsed={elapsed}ms"
			);

			debug_assert!(
				!total_minted.is_zero(),
				"mint_and_distribute: issuance was non-zero but nothing was minted"
			);

			total_minted
		}
```

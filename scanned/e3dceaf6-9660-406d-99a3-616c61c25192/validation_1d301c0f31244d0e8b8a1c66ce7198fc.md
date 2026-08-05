### Title
Spot-traffic multiplier collapses to the floor price instead of decaying gradually, enabling underpriced on-demand coretime purchases - (File: `polkadot/runtime/parachains/src/on_demand/mod.rs`)

### Summary
`Pallet::calculate_spot_traffic` computes the on-demand blockspace "traffic" multiplier that is used to derive the spot price paid for on-demand core purchases. The intended model (documented in the transaction-payment analog) is a second-order Taylor approximation `next = previous * (1 ± t1 + t2)`. In the "negative" branch (queue utilization below target), the implementation drops the `previous` multiplicative term and the `1` offset entirely, directly conflating the *delta* with the *new value*, which structurally mirrors the reported `_updateState` bug: an equation that fails to account for a scenario range and produces an out-of-bound/incorrect result instead of the intended monotonic, gradual adjustment.

### Finding Description
`calculate_spot_traffic` branches on whether `queue_util_ratio >= target_queue_utilisation` (`positive`): [1](#0-0) 

- In the `positive` branch, the code correctly implements `traffic * (1 + diff + div_by_two)` — it adds `One::one()` before multiplying by `traffic`, matching the documented Taylor formula.
- In the `else` (negative/under-utilized) branch, the code computes `queue_util_diff.saturating_sub(div_by_two).saturating_mul(traffic)` **without ever adding/subtracting the `1` term or subtracting the result from `traffic`**. Instead of computing `traffic * (1 - diff + div_by_two)` (the mathematically correct decaying formula, analogous to how `pallet-transaction-payment`'s `TargetedFeeAdjustment::convert` does `previous.saturating_sub(negative)` in its negative branch, see `substrate/frame/transaction-payment/src/lib.rs:265-272`), this branch directly returns `diff * traffic` (minus a tiny second-order term) as the *entire new traffic value*.

Because `diff = |queue_util_ratio - target_queue_utilisation|` is typically a small fraction (e.g. 0.01–0.2 in Perbill terms), multiplying it directly by `traffic` collapses the new traffic value to a tiny fraction of its previous value on almost any single block where utilization dips even slightly below target — regardless of how high `traffic` had climbed due to sustained prior demand. The subsequent `.max(TrafficDefaultValue::get())` clamp then snaps the result straight to the pallet's minimum floor value in virtually all cases, rather than letting the multiplier decay gradually as the documented model intends.

This is the direct structural analog of the reported bug: an interest/price formula meant to move monotonically and smoothly with a utilization ratio instead produces a value dominated by the wrong term for one side of the ratio's range, causing an economically incorrect (here: artificially deflated) result.

### Impact Explanation
The `traffic` value directly scales the on-demand spot price charged for parachain blockspace: `spot_price = new_traffic * on_demand_base_fee` (see `update_spot_traffic`, lines 578-581, `mod.rs`). An unprivileged actor who wants to buy on-demand core time cheaply can simply wait for (or induce, by not placing orders) a single block where the on-demand order queue utilization dips at or below `on_demand_target_queue_utilization`. On that block, any elevated `traffic` accumulated from a prior demand spike is discarded and reset to the pallet floor value, letting anyone purchase on-demand coretime immediately afterward at the minimum price even though genuine demand/congestion has not actually subsided by a proportional amount. This is public, underpriced work that degrades the intended congestion-pricing mechanism for block production resources on parachains using the on-demand assignment pallet (Coretime marketplace), matching the "public underpriced work that degrades block production" impact category.

### Likelihood Explanation
No privileged actor is required. Any account can call the public on-demand order placement extrinsic that triggers `update_spot_traffic` in `on_finalize`/order-processing path, and the queue utilization naturally fluctuates below target on essentially every block that isn't fully congested, since `queue_util_diff` need only exceed zero by a small perbill amount for the collapse to dominate. This makes the mispricing trivially and repeatedly triggerable under normal usage patterns, not just adversarial edge cases.

### Recommendation
Fix the negative branch to mirror the correct decaying formula used elsewhere in the codebase (e.g. `pallet-transaction-payment`'s `TargetedFeeAdjustment`): compute `negative_term = queue_util_diff.saturating_sub(div_by_two).saturating_mul(traffic)` and then return `traffic.saturating_sub(negative_term).max(TrafficDefaultValue::get())`, so that the multiplier decays proportionally from its previous value instead of being replaced by the delta term.

### Proof of Concept
1. Let `TrafficDefaultValue = 1.0`, `on_demand_target_queue_utilization = 25%`, current `traffic = 10.0` (accumulated from sustained high demand).
2. Queue utilization drops to 24% for one block (`queue_util_ratio < target`, so `positive = false`).
3. `queue_util_diff ≈ 0.01`, `div_by_two ≈ 0` (second-order term negligible).
4. Buggy code: `new_traffic = (0.01 - ~0) * 10.0 = 0.1`, then `.max(1.0) = 1.0`.
5. Correct formula would give `traffic * (1 - 0.01 + ~0) = 9.9`, i.e. traffic should barely change.
6. Result: the spot price for on-demand blockspace instantly resets to the floor price (`1.0 * on_demand_base_fee`) instead of `~9.9 * on_demand_base_fee`, letting any caller purchase coretime at close to the minimum price right after a demand spike with only a 1% dip in queue utilization. [2](#0-1)

### Citations

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L618-665)
```rust
	fn calculate_spot_traffic(
		traffic: FixedU128,
		queue_capacity: u32,
		queue_size: u32,
		target_queue_utilisation: Perbill,
		variability: Perbill,
	) -> Result<FixedU128, SpotTrafficCalculationErr> {
		// Return early if queue has no capacity.
		if queue_capacity == 0 {
			return Err(SpotTrafficCalculationErr::QueueCapacityIsZero);
		}

		// Return early if queue size is greater than capacity.
		if queue_size > queue_capacity {
			return Err(SpotTrafficCalculationErr::QueueSizeLargerThanCapacity);
		}

		// (queue_size / queue_capacity) - target_queue_utilisation
		let queue_util_ratio = FixedU128::from_rational(queue_size.into(), queue_capacity.into());
		let positive = queue_util_ratio >= target_queue_utilisation.into();
		let queue_util_diff = queue_util_ratio.max(target_queue_utilisation.into()) -
			queue_util_ratio.min(target_queue_utilisation.into());

		// variability * queue_util_diff
		let var_times_qud = queue_util_diff.saturating_mul(variability.into());

		// variability^2 * queue_util_diff^2
		let var_times_qud_pow = var_times_qud.saturating_mul(var_times_qud);

		// (variability^2 * queue_util_diff^2)/2
		let div_by_two: FixedU128;
		match var_times_qud_pow.const_checked_div(2.into()) {
			Some(dbt) => div_by_two = dbt,
			None => return Err(SpotTrafficCalculationErr::Division),
		}

		// traffic * (1 + queue_util_diff) + div_by_two
		if positive {
			let new_traffic = queue_util_diff
				.saturating_add(div_by_two)
				.saturating_add(One::one())
				.saturating_mul(traffic);
			Ok(new_traffic.max(<T as Config>::TrafficDefaultValue::get()))
		} else {
			let new_traffic = queue_util_diff.saturating_sub(div_by_two).saturating_mul(traffic);
			Ok(new_traffic.max(<T as Config>::TrafficDefaultValue::get()))
		}
	}
```

Audit Report

## Title
Spot-traffic multiplier collapses to the floor price instead of decaying gradually, enabling underpriced on-demand coretime purchases - (File: `polkadot/runtime/parachains/src/on_demand/mod.rs`)

## Summary
`Pallet::calculate_spot_traffic` implements the documented second-order Taylor approximation `next = previous * (1 ± t1 + t2)` only in its "positive" (queue at/above target) branch. In the "negative" branch it drops both the `previous`-scaled `1` term and the correct subtraction structure, instead directly computing `new_traffic = queue_util_diff.saturating_sub(div_by_two).saturating_mul(traffic)` and returning that as the entire new traffic value, rather than `traffic - (queue_util_diff - div_by_two) * traffic`.

## Finding Description
In `calculate_spot_traffic` [1](#0-0) , the `positive` branch correctly computes `traffic * (1 + queue_util_diff + div_by_two)` by adding `One::one()` before the multiplication by `traffic`. The `else` branch, however, computes `(queue_util_diff - div_by_two) * traffic` directly and returns that as `new_traffic`, never multiplying `traffic` by `(1 - queue_util_diff + div_by_two)` and never adding back the retained portion of `traffic`. Since `queue_util_diff` is a `Perbill`-derived fraction typically far below 1 (e.g., 0.01–0.2), this collapses `new_traffic` to a small fraction of `traffic` for essentially any utilization dip below target, after which `.max(TrafficDefaultValue::get())` snaps the result to the floor value in virtually all cases. This is confirmed directly by the code as shown; the positive branch's correct `saturating_add(One::one())` pattern is conspicuously absent from the negative branch, confirming the asymmetry described in the claim.

## Impact Explanation
`update_spot_traffic` uses `new_traffic` to compute `spot_price = new_traffic.saturating_mul_int(on_demand_base_fee)` [2](#0-1) . Because the negative branch discards accumulated `traffic` instead of decaying it, any account that places or is affected by on-demand orders can obtain artificially cheap spot pricing on virtually any block where queue utilization dips at or below `on_demand_target_queue_utilization`, even immediately after a genuine demand spike. This matches the "public underpriced work that degrades block production" impact category for coretime purchases.

## Likelihood Explanation
No privileged actor is required — queue utilization naturally fluctuates below target on most blocks that aren't fully congested, and any public actor interacting with the on-demand order pathway that triggers `update_spot_traffic` can observe/benefit from this behavior. The condition (`queue_util_ratio < target_queue_utilisation` by even a small margin) is trivially and repeatedly satisfied under normal usage, not merely an adversarial edge case.

## Recommendation
Fix the negative branch to mirror `pallet-transaction-payment`'s `TargetedFeeAdjustment::convert` pattern: compute `negative_term = queue_util_diff.saturating_sub(div_by_two).saturating_mul(traffic)`, then return `traffic.saturating_sub(negative_term).max(TrafficDefaultValue::get())`, so the multiplier decays proportionally from `traffic` rather than being replaced by the delta term.

## Proof of Concept
1. Set `TrafficDefaultValue = 1.0`, `on_demand_target_queue_utilization = 25%`, and let `traffic = 10.0` from sustained prior demand.
2. Queue utilization drops to 24% for one block, so `positive = false`, `queue_util_diff ≈ 0.01`, `div_by_two ≈ 0`.
3. Current code: `new_traffic = (0.01 − ~0) * 10.0 = 0.1`, then `.max(1.0) = 1.0` — traffic collapses to the floor.
4. Correct behavior would yield `traffic * (1 − 0.01 + ~0) ≈ 9.9`.
5. The resulting spot price is `1.0 * on_demand_base_fee` instead of `~9.9 * on_demand_base_fee`, letting any caller purchase coretime near the floor price after only a 1% utilization dip. This can be validated by adding a unit test to `polkadot/runtime/parachains/src/on_demand/tests.rs` that calls `Pallet::<Test>::calculate_spot_traffic` directly with these inputs and asserting the returned value is far below the expected decayed value.

### Citations

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L578-581)
```rust
					// calculate the new spot price
					let spot_price: BalanceOf<T> = new_traffic.saturating_mul_int(
						config.scheduler_params.on_demand_base_fee.saturated_into::<BalanceOf<T>>(),
					);
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L654-665)
```rust
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

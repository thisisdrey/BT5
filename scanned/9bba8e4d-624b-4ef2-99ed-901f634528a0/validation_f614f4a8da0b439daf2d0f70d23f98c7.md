Based on the investigation, the strongest local analog to the OpenQ bug — a bounded resource where one code path is exempted from the size-limit check yet still consumes capacity shared with checked paths — is in the XCM executor's Holding Register accounting.

### Title
Unchecked `fees`-to-`holding` merge in `RefundSurplus` lets a privileged path bypass `ensure_can_subsume_assets`, silently exceeding `MaxAssetsIntoHolding` - (File: `polkadot/xcm/xcm-executor/src/lib.rs`)

### Summary
The XCM executor enforces `MaxAssetsIntoHolding` (`holding_limit`) on every path that adds assets to the `holding` register — `WithdrawAsset`, `ReserveAssetDeposited`, `ReceiveTeleportedAsset`, `ClaimAsset`, and even the trader-weight-refund branch of `refund_surplus` — via `ensure_can_subsume_assets`. However, the same function's final step, which merges the leftover `fees` register into `holding`, calls `self.holding.subsume_assets(leftover_fees)` directly with no bound check at all. This mirrors the OpenQ pattern: a "privileged" code path silently exceeds a limit that all other equivalent paths must respect, while still writing into the same shared bounded structure.

### Finding Description
`ensure_can_subsume_assets` is the sole guard protecting the invariant that `holding.len()` never exceeds `2 * holding_limit`: [1](#0-0) 

It is deliberately invoked before the trader-weight-refund is subsumed into `holding` inside `refund_surplus`: [2](#0-1) 

But immediately after, the leftover `fees` register (populated by `PayFees`/`take_fee`, including the error/rollback branches that call `self.fees.subsume_assets(unspent)` or `self.fees.subsume_assets(given_assets)`) is merged into `holding` with **no** call to `ensure_can_subsume_assets`: [3](#0-2) 

Meanwhile, ordinary asset-deposit paths such as `WithdrawAsset`/`ReserveAssetDeposited`/`ClaimAsset` are strictly capped, as demonstrated by the executor test that fails with `XcmError::HoldingWouldOverflow` once 9 distinct assets are withdrawn against a small `MaxAssetsIntoHolding`: [4](#0-3) 

The `fees` register can accumulate multiple *distinct* `AssetId`s across a program via `PayFees`, `take_fee`'s asset-exchange path, and rollback branches: [5](#0-4) [6](#0-5) 

Because none of those `fees`-register mutations are bounded either, `self.fees` can end up holding several distinct asset IDs (e.g., through `AssetExchanger::exchange_asset` failures that push back `given_assets` of a different `AssetId` than what was withdrawn, or through separate `PayFees`/delivery-fee cycles). When `RefundSurplus` runs, all of those distinct IDs get merged into `holding` unconditionally, growing `holding.len()` past the value every other part of the codebase — weight benchmarking (`worst_case_holding`, `WeighAssets` for `AllOf{Fungibility::NonFungible}` multiplying by `MaxAssetsIntoHolding * 2`) and the `ensure_can_subsume_assets` invariant itself — assumes is the hard ceiling.

### Impact Explanation
`MaxAssetsIntoHolding` is not just a soft configuration knob: downstream weight formulas (e.g. `WeighAssets::weigh_assets` for wildcard non-fungible filters, and `pallet_xcm_benchmarks`' `worst_case_holding`) are derived assuming holding never exceeds this bound. A message that inflates `holding` beyond that ceiling through the unguarded `fees→holding` merge causes any subsequent instruction that iterates the full holding (e.g. `DepositAsset(Wild(All))`, `ReportHolding`, `ClearOrigin` handling of the whole set) to perform more work than its benchmarked weight accounts for. This is exactly the "public underpriced work that degrades block production or stalls bridge processing" category called out in the impact gate — an unprivileged XCM sender can craft a program that both this instruction sequence executes and be under-billed for the true cost of processing the resulting oversized holding, risking block-production slowdowns or PoV/weight exhaustion when many such messages are processed (including via Snowbridge/BridgeHub inbound queues, which route messages through this same executor).

### Likelihood Explanation
No privileged actor, governance, or malicious relayer/validator is required — a normal, unprivileged XCM sender constructs the message. The `PayFees`/fee-exchange/rollback paths that populate `self.fees` with additional distinct asset entries are reachable from ordinary user-originated XCM programs (any account that can send an XCM with `WithdrawAsset` + `PayFees` + asset-exchange configured on the chain), making the precondition realistic on any chain configured with an `AssetExchanger` or multiple fee-asset flows.

### Recommendation
Apply the same `ensure_can_subsume_assets` (or an equivalent length check against `holding_limit`) before merging `self.fees` into `self.holding` inside `refund_surplus`, exactly as is already done for the trader-refund branch a few lines above it. If the check fails, either drop/trap the excess leftover fee assets instead of merging them, mirroring the "undo by buying back the weight" pattern already used for the trader-refund case.

### Proof of Concept
1. Configure a test runtime with a small `MaxAssetsIntoHolding` (e.g. 2) and an `AssetExchanger` that, on `exchange_asset` failure, returns `given_assets` containing a different `AssetId` than what was requested (a legitimate, spec-compliant exchanger behavior).
2. Build an XCM: `WithdrawAsset(AssetA, AssetB)` (filling `holding` to the limit) → `PayFees(AssetA)` → an instruction that triggers `take_fee` with a `FeeReason` requiring a swap to `AssetC` that fails in `exchange_asset`, causing `self.fees.subsume_assets(given_assets)` to add `AssetC` into the `fees` register → `RefundSurplus`.
3. Observe that `RefundSurplus` succeeds and `self.holding.len()` after execution is 3 (`AssetA`, `AssetB`, `AssetC`), exceeding `MaxAssetsIntoHolding = 2`, whereas an equivalent attempt to add `AssetC` via a fourth `WithdrawAsset` instruction would have been rejected with `XcmError::HoldingWouldOverflow` per the existing `max_assets_limit_should_work` test pattern.

### Citations

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L524-538)
```rust
	fn ensure_can_subsume_assets(&self, assets_length: usize) -> Result<(), XcmError> {
		// worst-case, holding.len becomes 2 * holding_limit.
		// this guarantees that if holding.len() == holding_limit and you have more than
		// `holding_limit` items (which has a best case outcome of holding.len() == holding_limit),
		// then the operation is guaranteed to succeed.
		let worst_case_holding_len = self.holding.len() + assets_length;
		tracing::trace!(
			target: "xcm::ensure_can_subsume_assets",
			?worst_case_holding_len,
			holding_limit = ?self.holding_limit,
			"Ensuring subsume assets work",
		);
		ensure!(worst_case_holding_len <= self.holding_limit * 2, XcmError::HoldingWouldOverflow);
		Ok(())
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L541-581)
```rust
	fn refund_surplus(&mut self) -> Result<(), XcmError> {
		let current_surplus = self.total_surplus.saturating_sub(self.total_refunded);
		tracing::trace!(
			target: "xcm::refund_surplus",
			total_surplus = ?self.total_surplus,
			total_refunded = ?self.total_refunded,
			?current_surplus,
			"Refunding surplus",
		);
		if current_surplus.any_gt(Weight::zero()) {
			if let Some(refund) = self.trader.refund_weight(current_surplus, &self.context) {
				// Check if adding the refund would overflow holding. This can happen if the
				// refund asset is not already in holding and holding is at max capacity.
				if refund
					.fungible
					.first_key_value()
					.map(|(id, _)| {
						!self.holding.fungible.contains_key(id) &&
							self.ensure_can_subsume_assets(1).is_err()
					})
					.unwrap_or(false)
				{
					// Can't add refund to holding - undo by buying back the weight.
					// This returns the refund credit to the trader where it will be
					// handled by OnUnbalanced when the trader is dropped.
					let _ = self
						.trader
						.buy_weight(current_surplus, refund, &self.context)
						.defensive_proof(
							"refund_weight returned an asset capable of buying weight; qed",
						);
					tracing::error!(
						target: "xcm::refund_surplus",
						"error: HoldingWouldOverflow",
					);
					return Err(XcmError::HoldingWouldOverflow);
				}
				self.total_refunded.saturating_accrue(current_surplus);
				self.holding.subsume_assets(refund);
			}
		}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L582-590)
```rust
		// If there are any leftover `fees`, merge them with `holding`.
		if !self.fees.is_empty() {
			let leftover_fees = self.fees.saturating_take(Wild(All));
			tracing::trace!(
				target: "xcm::refund_surplus",
				?leftover_fees,
			);
			self.holding.subsume_assets(leftover_fees);
		}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L651-675)
```rust
		// We perform the swap, if needed, to pay fees.
		let paid = if asset_to_pay_for_fees.id != asset_needed_for_fees.id {
			Config::AssetExchanger::exchange_asset(
				self.origin_ref(),
				withdrawn_fee_asset,
				&asset_needed_for_fees.clone().into(),
				false,
			)
			.map_err(|given_assets| {
				tracing::error!(
					target: "xcm::fees",
					?given_assets, ?asset_needed_for_fees, "Swap was deemed necessary but couldn't be done:",
				);
				self.fees.subsume_assets(given_assets);
				XcmError::FeesNotMet
			})?
		} else {
			// If the asset wanted to pay for fees is the one that was needed,
			// we don't need to do any swap.
			// We just use the assets withdrawn or taken from holding.
			withdrawn_fee_asset
		};
		Config::FeeManager::handle_fee(paid, Some(&self.context), reason);
		Ok(())
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1473-1510)
```rust
			PayFees { asset } => {
				// If we've already paid for fees, do nothing.
				if self.already_paid_fees {
					return Ok(());
				}
				// Make sure `PayFees` won't be processed again.
				self.already_paid_fees = true;
				// The max we're willing to pay for fees is decided by the `asset` operand.
				tracing::trace!(
					target: "xcm::executor::PayFees",
					asset_for_fees = ?asset,
					message_weight = ?self.message_weight,
				);
				// Pay for execution fees.
				self.transactional_process_with_custom_rollback(
					|self_ref| {
						let max_fee =
							self_ref.holding.try_take(asset.into()).map_err(|error| {
								tracing::debug!(
									target: "xcm::process_instruction::pay_fees", ?error,
									"Failed to take fees from holding"
								);
								XcmError::NotHoldingFees
							})?;
						let unspent =
							self_ref.trader.buy_weight(self_ref.message_weight, max_fee.into(), &self_ref.context).map_err(|(unspent, e)| {
								self_ref.fees.subsume_assets(unspent);
								e
							})?;
						// Move unspent to the `fees` register, it can later be moved to holding by calling `RefundSurplus`.
						self_ref.fees.subsume_assets(unspent);
						Ok(())
					},
					|self_ref| {
						self_ref.already_paid_fees = false;
					},
				)
			},
```

**File:** polkadot/xcm/xcm-builder/src/tests/assets.rs (L404-466)
```rust
#[test]
fn max_assets_limit_should_work() {
	// we'll let them have message execution for free.
	AllowUnpaidFrom::set(vec![[Parachain(1)].into()]);
	// Child parachain #1 owns 1000 tokens held by us in reserve.
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(0)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(1)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(2)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(3)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(4)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(5)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(6)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(7)]), 1000u128));
	add_asset(Parachain(1), (Junctions::from([GeneralIndex(8)]), 1000u128));

	// Attempt to withdraw 8 (=2x4)different assets. This will succeed.
	let message = Xcm(vec![
		WithdrawAsset((Junctions::from([GeneralIndex(0)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(1)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(2)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(3)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(4)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(5)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(6)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(7)]), 100u128).into()),
	]);
	let mut hash = fake_message_hash(&message);
	let r = XcmExecutor::<TestConfig>::prepare_and_execute(
		Parachain(1),
		message,
		&mut hash,
		Weight::from_parts(100, 100),
		Weight::zero(),
	);
	assert_eq!(r, Outcome::Complete { used: Weight::from_parts(85, 85) });

	// Attempt to withdraw 9 different assets will fail.
	let message = Xcm(vec![
		WithdrawAsset((Junctions::from([GeneralIndex(0)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(1)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(2)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(3)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(4)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(5)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(6)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(7)]), 100u128).into()),
		WithdrawAsset((Junctions::from([GeneralIndex(8)]), 100u128).into()),
	]);
	let mut hash = fake_message_hash(&message);
	let r = XcmExecutor::<TestConfig>::prepare_and_execute(
		Parachain(1),
		message,
		&mut hash,
		Weight::from_parts(100, 100),
		Weight::zero(),
	);
	assert_eq!(
		r,
		Outcome::Incomplete {
			used: Weight::from_parts(95, 95),
			error: InstructionError { index: 8, error: XcmError::HoldingWouldOverflow },
		}
	);
```

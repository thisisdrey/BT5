This claim is verified against the actual code. Both files exist exactly as cited, and the described behavior matches precisely.

Audit Report

## Title
`pallet-accumulate-and-forward` reports `ForwardSucceeded` and irreversibly burns treasury funds on local XCM completion, even though remote deposit/settlement can fail and trap the assets permanently - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`, `substrate/frame/accumulate-and-forward/src/lib.rs`)

## Summary
`TeleportForwarderForAccountId32::forward` treats `Outcome::Complete` from the local `XcmExecutor::prepare_and_execute` call as final success, even though this only confirms that local instructions (`WithdrawAsset`, `DescendOrigin`, `InitiateTransfer`) executed correctly and says nothing about whether the remote `DepositAsset` on the destination chain succeeds. `on_idle` in `pallet_accumulate_and_forward` then emits `Event::ForwardSucceeded` and permanently burns the funds locally, with no retry or reconciliation if the destination-side deposit fails.

## Finding Description
`forward()` in [1](#0-0)  constructs an XCM program (`UnpaidExecution → DescendOrigin → WithdrawAsset → InitiateTransfer` with a `remote_xcm` of `DepositAsset { beneficiary: StagingLocation }`), executes it locally inside `with_transaction`, and commits `Ok(())` purely on `Outcome::Complete`. The doc comment on the adapter itself states: "Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path" [2](#0-1) .

The pallet's `on_idle` hook consumes this local-only success signal and finalizes state without any remote confirmation: [3](#0-2) . The crate-level doc comment confirms the burn-then-mint model is asynchronous and unverified: "Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same funds are minted at the destination when the sent message is received" [4](#0-3) . There is no code path in either file that observes or reacts to destination-side execution results, and the only rollback tested is for local router/executor failure, per [5](#0-4) .

## Impact Explanation
This affects every chain wiring `pallet_accumulate_and_forward::Config::Forwarder = TeleportForwarderForAccountId32<...>` (confirmed present in Westend relay chain and multiple Westend system parachain runtime configs via `xcm_config.rs` references found in the repo). A destination-side rejection of the teleported deposit (e.g., staging account issue, barrier rejection, sufficiency/registration mismatch, weight/version issues) results in funds burned on the source chain, an event stating success, and no automated path to recover or reconcile the trapped remote assets — a duplicate/never-settled payout state and fund-lock condition.

## Likelihood Explanation
This requires no privileged or malicious actor; it is triggered by ordinary asynchronous XCM failure modes on the destination chain that are outside the sending chain's control, and the `on_idle` hook runs autonomously and repeatedly every `TransferPeriod`, making this a systemic architectural gap rather than a rare edge case.

## Recommendation
Do not treat local `Outcome::Complete` as final settlement. Require a destination-side settlement confirmation (e.g., a delivery/execution receipt analogous to bridge relayer receipt patterns) before emitting `ForwardSucceeded`, or keep local funds in an "in-flight" reconciliation state, or wire an automated `AssetsTrapped` claim-back path credited to the accumulation account.

## Proof of Concept
1. Configure a runtime with `Forwarder = TeleportForwarderForAccountId32<...>` targeting a destination whose `StagingLocation` account/`Barrier` will reject the incoming `UnpaidExecution`/`DepositAsset`.
2. Fund the accumulation account above `MinTransferAmount`; advance to a block that is a multiple of `TransferPeriod`.
3. `on_idle` ( [6](#0-5) ) invokes `Forwarder::forward`, which returns `Ok(())` because local execution completes (`Outcome::Complete`).
4. `Event::ForwardSucceeded` is emitted and the accumulation account balance is permanently reduced.
5. On the destination, the `DepositAsset` instruction fails, and the incoming assets are recorded as `AssetsTrapped` rather than credited to the staging account, with no automated recovery — reproducing the fund-loss/duplicate-settlement-state condition described.

### Citations

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L34-42)
```rust
/// XCM adapter that implements [`pallet_accumulate_and_forward::Forwarder`] for AccountId32-type
/// source accounts by teleporting native tokens to a target account on a destination chain.
/// Local-execution failures roll back all local state changes. Once the local executor reports
/// success, the message is queued and any destination-side rejection results in trapped assets
/// at the destination with no automatic recovery path.
///
/// NOTE: This adapter passes `Weight::MAX` to the XCM executor, relying on the call site to
/// enforce a weight budget before invoking it. It is designed to be called only from rate-limited
/// internal hooks such as `on_idle` and should never be wired to user-callable extrinsics.
```

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L58-106)
```rust
	fn forward(source: AccountId, amount: Balance) -> Result<(), ()> {
		let dest = Dest::get();
		let asset = Asset { id: AssetId(NativeAsset::get()), fun: Fungible(amount.into()) };
		let beneficiary: Location = StagingLocation::get().into_location();

		let remote_xcm = Xcm(vec![DepositAsset { assets: Wild(AllCounted(1)), beneficiary }]);

		// The XCM flow: `ReceiveTeleportedAsset → AliasOrigin(source) → UnpaidExecution →
		// DepositAsset`. `preserve_origin: true` causes `InitiateTransfer` to prepend
		// `AliasOrigin(source_location)` to the remote XCM.
		let xcm: Xcm<XcmConfig::RuntimeCall> = Xcm(vec![
			UnpaidExecution { weight_limit: WeightLimit::Unlimited, check_origin: None },
			DescendOrigin(Junction::AccountId32 { network: None, id: source.into() }.into()),
			WithdrawAsset(asset.into()),
			InitiateTransfer {
				destination: dest,
				remote_fees: None,
				preserve_origin: true,
				assets: BoundedVec::truncate_from(alloc::vec![AssetTransferFilter::Teleport(
					Wild(AllCounted(1))
				),]),
				remote_xcm,
			},
		]);

		with_transaction(|| -> TransactionOutcome<Result<(), DispatchError>> {
			let outcome = XcmExecutor::<XcmConfig>::prepare_and_execute(
				Location::here(),
				xcm,
				&mut [0u8; 32],
				Weight::MAX,
				Weight::MAX,
			);

			match outcome {
				Outcome::Complete { .. } => TransactionOutcome::Commit(Ok(())),
				exec_error => {
					tracing::debug!(
						target: LOG_TARGET,
						?exec_error,
						"accumulate-forward: XCM execution failed"
					);

					TransactionOutcome::Rollback(Err(DispatchError::Other("XCM execution failed")))
				},
			}
		})
		.map_err(|_| ())
	}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L40-43)
```rust
//! ## Total Issuance
//!
//! Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same
//! funds are minted at the destination when the sent message is received.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L152-201)
```rust
		fn on_idle(_block: SystemBlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			// Only attempt forwarding on blocks that are exact multiples of `TransferPeriod`.
			let block = T::BlockNumberProvider::current_block_number();
			if (block % T::TransferPeriod::get()) != Zero::zero() {
				return Weight::zero();
			}

			let mut meter = WeightMeter::with_limit(remaining_weight);

			// Need one read for the balance check.
			if meter.try_consume(T::DbWeight::get().reads(1)).is_err() {
				return meter.consumed();
			}

			let accumulation_account = Self::accumulation_account();
			// We use `reducible_balance` with `Preservation::Preserve` to get the
			// usable balance (excluding the ED).
			let available_funds = T::Currency::reducible_balance(
				&accumulation_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available_funds < T::MinTransferAmount::get() {
				return meter.consumed();
			}

			// Ensure there is enough weight budget for the full XCM send.
			if meter.try_consume(T::WeightInfo::send_native()).is_err() {
				return meter.consumed();
			}

			// Attempt to forward accumulated funds.
			match T::Forwarder::forward(accumulation_account, available_funds) {
				Ok(()) => {
					Self::deposit_event(Event::ForwardSucceeded { amount: available_funds });
				},
				Err(()) => {
					log::debug!(
						target: LOG_TARGET,
						"accumulate-forward transfer of {:?} failed at block {:?}",
						available_funds,
						block,
					);
					Self::deposit_event(Event::ForwardFailed { amount: available_funds });
				},
			}

			meter.consumed()
		}
```

**File:** polkadot/xcm/xcm-builder/src/tests/forwarder.rs (L169-200)
```rust
/// Verify that when the XCM router fails (simulating a mid-program failure after `WithdrawAsset`
/// has debited the source account), `forward` rolls back all storage changes via
/// `with_transaction`. Both the source balance and total issuance must remain unchanged.
#[test]
fn forward_rolls_back_balance_and_issuance_on_xcm_failure() {
	let source: AccountId = AccountId32::from([1u8; 32]);
	let initial_balance = 1_000u128;

	new_test_ext(source.clone(), initial_balance).execute_with(|| {
		let initial_issuance = Balances::total_issuance();

		ROUTER_SHOULD_FAIL.with(|f| f.set(true));
		let result = TeleportForwarderForAccountId32::<
			TestXcmConfig,
			AssetHubLocation,
			HereLocation,
			AccumulationInterior,
		>::forward(source.clone(), 500u128);
		ROUTER_SHOULD_FAIL.with(|f| f.set(false));

		assert!(result.is_err(), "expected forward to fail when router fails");
		assert_eq!(
			Balances::balance(&source),
			initial_balance,
			"source balance must be unchanged after XCM failure"
		);
		assert_eq!(
			Balances::total_issuance(),
			initial_issuance,
			"total issuance must be unchanged after XCM failure"
		);
	});
```

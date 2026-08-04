## Title
Unbounded single-shot teleport in `pallet-accumulate-and-forward` can permanently trap accumulated protocol funds with no recovery path - (File: `polkadot/xcm/xcm-builder/src/forwarder.rs`)

### Summary
`pallet-accumulate-and-forward` collects transaction fees, dust removals, and coretime revenue into a pallet-derived accumulation account with no cap on how much can build up during a `TransferPeriod` [1](#0-0) . At the period boundary, `on_idle` reads the *entire* reducible balance and hands it to `T::Forwarder::forward()` as a single, non-divisible amount [2](#0-1) . The production `Forwarder` implementation, `TeleportForwarderForAccountId32`, burns the whole amount locally and teleports it in one XCM message using `Weight::MAX` for local execution, with the destination-side `DepositAsset` also unmetered/unpaid [3](#0-2) . The code's own doc comment concedes: "any destination-side rejection results in trapped assets at the destination with no automatic recovery path" [4](#0-3) . This is the direct structural analog of the GammaSwap report: unbounded per-period accumulation is funneled into one downstream operation with a hard capacity/behavioral limit, and there is no cap or amount-splitting mechanism to keep the batch within safe bounds.

### Finding Description
The core broken invariant mirrors the seed report exactly: deposits accumulate without any per-period cap (`depositVault` in the seed vs. the accumulation account here, fed by `OnUnbalanced` fee/dust/coretime hooks [5](#0-4) ), and the entire accumulated amount is then pushed through a single downstream call whose success is not guaranteed to scale with the batch size.

Two concrete failure paths exist because of this uncapped, single-shot design:

1. **Repeated permanent failure (matches the seed's `borrowLiquidity` revert pattern).** If the forward fails for any reason (e.g., the destination-side execution — `UnpaidExecution`/`DepositAsset` at the DAP staging location on AssetHub — cannot process the message, due to weight exhaustion at destination, an unrecognized/blocked asset filter mismatch, or any other transient destination condition), `on_idle` emits `ForwardFailed` and leaves the funds in place [6](#0-5) . Because the balance is *never capped or reduced* on failure, and only *grows* between periods, the amount attempted on the next period is the same or larger, so a persistent destination-side rejection condition becomes a **permanent stall of fee/dust/revenue routing** — the analogous DoS to the GammaSwap `_increaseHedge`/`borrowLiquidity` revert loop.

2. **Fund lock with no recovery (worse than the seed bug — actual loss, not just DoS).** Unlike GammaSwap's revert (which safely aborts before any state change), here local execution *commits* (burns the source balance) before the remote leg is guaranteed to succeed: "Local-execution failures roll back... Once the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path" [4](#0-3) . Since the whole period's accumulated balance is sent as one indivisible unit, a destination-side rejection (e.g., `DepositAsset`'s `Wild(AllCounted(1))` filter not matching what actually lands in holding, or any deviation between what was withdrawn locally and what the remote executor expects) traps the **entire batch**, not just the excess — this is a full "permanent user-fund lock," which is explicitly called out as within the accepted impact scope.

Existing guards do not stop this path:
- `MinTransferAmount` only bounds the *floor*, never the *ceiling*, of what gets forwarded [7](#0-6) .
- `TransferPeriod` only rate-limits *how often* a forward is attempted, not how large the batch is [8](#0-7) .
- The weight budget check (`meter.try_consume(T::WeightInfo::send_native())`) only guards the *local* `on_idle` weight accounting; the forwarder itself invokes the XCM executor with `Weight::MAX` for both local and remote legs, so there is no weight-based nor amount-based safety valve inside `forward()` itself [9](#0-8) .

### Impact Explanation
This satisfies the "permanent user-fund or bridge-state lock" and "balances... must conserve value and settle exactly once" pivots: protocol-level accumulated funds (fees, dust, coretime revenue destined for the DAP/treasury buffer on AssetHub) can be burned locally yet never credited at the destination, with the pallet providing no automated retry, refund, or reconciliation mechanism for trapped assets. Because the batch is periodic and grows unboundedly, once a destination-side condition triggers a rejection, it can recur period after period, compounding losses and simultaneously starving the destination DAP buffer of expected inflows — a direct chain-level impact via broken treasury/reward routing across multiple system parachains (asset-hub, bridge-hub, collectives, coretime, people runtimes are all wired to this exact `Forwarder`) [10](#0-9) [11](#0-10) [12](#0-11) .

### Likelihood Explanation
Medium. No malicious actor, governance, or privileged access is required — the accumulation account is populated automatically by ordinary chain activity (transaction fees, dust reaps, coretime revenue) with no attacker interaction needed to grow the batch size [13](#0-12) . The failure trigger itself (a destination-side rejection under an unbounded, ever-larger, atomically-all-or-nothing teleport) is exactly the condition the code's own doc comment flags as unrecoverable, indicating the authors are aware this is a live risk rather than a theoretical one.

### Recommendation
- Cap the maximum amount forwarded per period (mirroring the seed's recommendation to cap deposits per period), splitting any excess across additional forward attempts instead of sending the whole balance atomically.
- Add an explicit weight/asset-size ceiling inside `TeleportForwarderForAccountId32::forward()` rather than relying on `Weight::MAX`, so the local call site's weight budget check in `on_idle` is a meaningful backstop.
- Implement a recovery path for trapped assets (e.g., leveraging `AssetTraps`/`pallet_xcm::claim_assets`) that the accumulate-and-forward pallet or a dedicated recovery process can invoke automatically, rather than relying on manual, ad hoc intervention.

### Proof of Concept
1. Let ordinary chain activity (fees, dust, coretime revenue) accumulate in the accumulation account for several `TransferPeriod`s without ever reaching a successful forward (e.g., destination AssetHub message queue backlogged, or the beneficiary `DapStagingLocation` sub-account temporarily unable to accept the deposit).
2. At each period boundary, `on_idle` reads the full `reducible_balance` (now larger than before) and calls `Forwarder::forward(accumulation_account, available_funds)` [2](#0-1) .
3. If the remote-side `DepositAsset { assets: Wild(AllCounted(1)), beneficiary }` step fails for any reason after `WithdrawAsset`/`InitiateTransfer` locally succeeds and commits [14](#0-13) , the entire teleported balance for that period becomes a trapped asset at the destination with no automatic recovery, as acknowledged directly in the adapter's documentation [4](#0-3) .
4. If instead the local `prepare_and_execute` itself fails (e.g. any local-side XCM barrier/weight issue), the transaction rolls back and the batch retries next period with an even larger amount, reproducing the seed report's "revert loop that only grows worse over time" pattern, verified by the pallet's own failure-path test which shows `ForwardFailed` leaving the balance untouched for the next period's (larger) attempt [15](#0-14) .

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L18-43)
```rust
//! # Accumulate-and-Forward Pallet
//!
//! Intercepts configurable token inflows (transaction fees, dust removal, coretime revenue) on
//! system parachains and gathers them in a local accumulation account for periodic forwarding
//! to a configurable destination.
//!
//! ## Usage
//!
//! - **Fees**: Use [`DealWithFeesSplit`] to split fees between accumulation and other handlers
//! - **Burns/Revenue**: Use the pallet as `OnUnbalanced<CreditOf>` handler (e.g., dust removal,
//!   coretime revenue)
//! Note: Direct calls to `pallet_balances::Pallet::burn()` extrinsic are not redirected to
//! the accumulation account — they still reduce total issuance directly.
//!
//! ## Setup
//!
//! The accumulation account must be pre-funded with at least the existential deposit.
//! For new chains, include the account in the balances genesis config.
//! For existing chains, fund it via a manual transfer.
//!
//! If the accumulation account is not pre-funded, deposits below ED will be silently burned.
//!
//! ## Total Issuance
//!
//! Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same
//! funds are minted at the destination when the sent message is received.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L121-124)
```rust
		/// Minimum number of blocks between successive forwards.
		/// Acts as a rate limiter to avoid sending too many messages.
		#[pallet::constant]
		type TransferPeriod: Get<BlockNumberFor<Self>>;
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L126-130)
```rust
		/// Minimum transferable balance required to trigger a forward.
		/// This avoids forwarding very small / negligible amounts.
		/// The accumulation account always retains its existential deposit on top of this.
		#[pallet::constant]
		type MinTransferAmount: Get<BalanceOf<Self>>;
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L166-198)
```rust
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
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L254-279)
```rust
impl<T, AccumulatedPercent, OtherHandler> OnUnbalanced<CreditOf<T>>
	for DealWithFeesSplit<T, AccumulatedPercent, OtherHandler>
where
	T: Config,
	AccumulatedPercent: Get<Percent>,
	OtherHandler: OnUnbalanced<CreditOf<T>>,
{
	fn on_unbalanceds(mut fees_then_tips: impl Iterator<Item = CreditOf<T>>) {
		if let Some(fees) = fees_then_tips.next() {
			let accumulated_percent = AccumulatedPercent::get();
			let other_percent = Percent::one().saturating_sub(accumulated_percent);
			let mut split = fees.ration(
				accumulated_percent.deconstruct() as u32,
				other_percent.deconstruct() as u32,
			);
			if let Some(tips) = fees_then_tips.next() {
				// Tips go 100% to other handler.
				tips.merge_into(&mut split.1);
			}
			if !accumulated_percent.is_zero() {
				<Pallet<T> as OnUnbalanced<_>>::on_unbalanced(split.0);
			}
			OtherHandler::on_unbalanced(split.1);
		}
	}
}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L289-309)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let accumulation_account = Self::accumulation_account();
		let numeric_amount = amount.peek();

		// Resolve should never fail because:
		// - can_deposit on destination succeeds assuming accumulation account is pre-funded with ED
		// - amount is guaranteed non-zero by the trait method signature
		// The only failure would be overflow on destination or unfunded account.
		let _ = T::Currency::resolve(&accumulation_account, amount).inspect_err(|_| {
			frame_support::defensive!(
				"🚨 Failed to deposit to accumulation account - funds burned, it should never happen!"
			);
		});

		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to accumulation account"
		);
	}
}
```

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

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L58-90)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/lib.rs (L577-590)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		testnet_parachains_constants::westend::locations::AssetHubLocation,
		xcm_config::WestendLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = RelaychainDataProvider<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```

**File:** cumulus/parachains/runtimes/collectives/collectives-westend/src/lib.rs (L698-711)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		xcm_config::AssetHub,
		xcm_config::WndLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = RelaychainDataProvider<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```

**File:** polkadot/runtime/westend/src/lib.rs (L1356-1369)
```rust
impl pallet_accumulate_and_forward::Config for Runtime {
	type Currency = Balances;
	type PalletId = AccumulateForwardPalletId;
	type Forwarder = xcm_builder::TeleportForwarderForAccountId32<
		xcm_config::XcmConfig,
		xcm_config::AssetHub,
		xcm_config::TokenLocation,
		DapStagingLocation,
	>;
	type TransferPeriod = ForwardPeriod;
	type MinTransferAmount = MinForwardAmount;
	type BlockNumberProvider = frame_system::Pallet<Runtime>;
	type WeightInfo = weights::pallet_accumulate_and_forward::WeightInfo<Runtime>;
}
```

**File:** substrate/frame/accumulate-and-forward/src/tests/xcm_transfer.rs (L196-225)
```rust
// Check the failure path: when a forward fails, a `ForwardFailed` event is emitted
// and the accumulation balance is unchanged (mock does not withdraw).
#[test]
fn verify_failure_path() {
	new_test_ext(true).execute_with(|| {
		let period = TransferPeriod::get();
		let acc = get_accumulation_account();
		let funds = 50u64;

		reset_send_count();
		reset_last_sent_amount();
		fund_accumulation_account(funds);

		System::set_block_number(period);
		SEND_FAIL.with(|f| *f.borrow_mut() = true);

		let balance_before = Balances::free_balance(acc);
		let issuance_before = Balances::total_issuance();

		AccumulateForwardPallet::on_idle(period, Weight::from_all(u64::MAX));

		assert_eq!(get_send_count(), 0);
		assert_eq!(get_last_sent_amount(), None);
		assert_eq!(Balances::free_balance(acc), balance_before);
		assert_eq!(Balances::total_issuance(), issuance_before);
		System::assert_has_event(Event::<Test>::ForwardFailed { amount: funds }.into());

		SEND_FAIL.with(|f| *f.borrow_mut() = false);
	});
}
```

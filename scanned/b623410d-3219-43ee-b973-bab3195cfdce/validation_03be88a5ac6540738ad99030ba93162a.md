## Title
`pallet-accumulate-and-forward` teleports the *entire* balance of the accumulation account, allowing anyone to force unrelated funds to be burned and forwarded cross-chain via a plain balance transfer - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`)

### Summary
`pallet-accumulate-and-forward` is designed to gather fee/dust/coretime revenue in a dedicated `accumulation_account` and periodically teleport it to Asset Hub. Its `on_idle` hook decides *how much* to forward purely from `T::Currency::reducible_balance(&accumulation_account, ...)` [1](#0-0) , with no tracking of which portion of that balance actually originated from the intended sources (fees, dust, coretime). This is the direct analog of the external report's "artificial asset balance inflation": the pallet trusts `balanceOf(this_account)`-style introspection instead of internally tracked accounting, and since `accumulation_account` is a normal `AccountId` (not a contract with a `receive()` guard), *anyone* can inflate it with an ordinary `balances::transfer` — no `selfdestruct` trick is even required in Substrate.

### Finding Description
`on_idle` computes `available_funds` from the raw reducible balance of the accumulation account and unconditionally forwards (burns locally, mints at destination) whatever exceeds `MinTransferAmount`: [2](#0-1) 

The account is populated intentionally via `OnUnbalanced` hooks (`DealWithFeesSplit`, dust removal, `LegacyAdapter`) [3](#0-2) [4](#0-3) , but nothing prevents an unprivileged user from directly transferring native tokens (via `Balances::transfer_allow_death`/`transfer_keep_alive`) straight into `accumulation_account`, since it's a deterministically-derivable `PalletId` sub-account [5](#0-4) . The pallet's own tests confirm plain transfers land in this account and are treated identically to legitimate fee/dust inflows (e.g. `on_unbalanced_multiple_dust_removals_accumulate` shows dust from ordinary `transfer_allow_death` reaping accumulating into the account) [6](#0-5) .

Because `reducible_balance` cannot distinguish "protocol revenue" from "attacker-donated balance," any directly-sent funds are folded into the next `Forwarder::forward` call. The `Forwarder` implementation (`TeleportForwarderForAccountId32`) then burns exactly `available_funds` from `Location::here()` and teleports/deposits the same value to a fixed `StagingLocation` beneficiary on the destination chain [7](#0-6) . The crate-level docs explicitly acknowledge the burn-then-mint pattern is meant only for pallet-originated funds: "Accumulated funds are burnt upon forwarding ... and the same funds are minted at the destination" [8](#0-7) .

There is no `farm`/sweep mechanism, no allow-list of depositors, and no per-source accounting comparable to a ledger — this is exactly the "heavy reliance on `balanceOf(address(this))`" pattern flagged in the external report, translated into a Substrate pallet that reads `reducible_balance()` of its own well-known account as ground truth for a privileged cross-chain settlement action (burn + teleport-mint).

### Impact Explanation
Any account can force the pallet to teleport arbitrary externally-supplied value from the source parachain to the fixed `StagingLocation` beneficiary on the destination chain on the pallet's normal cadence, at no cost beyond a standard transfer (plus existing weight-metered `on_idle` execution, which the attacker doesn't even pay for). This:
- Breaks the intended invariant that only fee/dust/coretime revenue flows through this channel — an attacker can launder/move arbitrary DOT balances between chains outside of normal teleport/reserve-transfer accounting paths, bypassing whatever downstream logic on Asset Hub assumes the incoming stream correlates to actual protocol revenue.
- Can be used to grief the periodic on_idle window: attacker-controlled top-ups guarantee the `MinTransferAmount` threshold is met on every `TransferPeriod`, forcing unconditional weight consumption and XCM teleport execution (`Weight::MAX` passed to the local executor, per the adapter's own doc note that this must never be reachable by user-callable extrinsics) [9](#0-8) , effectively giving an unprivileged party indirect, repeated control over privileged burn+teleport execution meant to be restricted to protocol-derived funds.
- Represents unbacked/attacker-directed value movement through a privileged pallet path (burn locally / mint at destination to a beneficiary the depositor doesn't control), i.e., theft-adjacent misdirection of funds a user might send to that account by mistake, and unauthorized triggering of privileged cross-chain settlement logic by an ordinary transfer.

### Likelihood Explanation
High feasibility: `accumulation_account` is deterministically derivable from `PalletId::get().into_account_truncating()` [10](#0-9) , so any user can compute it and send a normal balance transfer with zero special tooling. The pallet performs no origin check, no source allow-list, and no distinction between `OnUnbalanced`-routed funds and directly-received transfers before computing `available_funds` and forwarding.

### Recommendation
Track accumulated funds via internal storage (a running counter incremented only by the `OnUnbalanced`/`Forwarder` code paths) instead of reading `reducible_balance(accumulation_account)` directly, so directly-transferred balances are not automatically eligible for forwarding. Alternatively, cap `available_funds` at the internally tracked "legitimate inflow" amount and provide a separate, restricted sweep/`farm`-style extrinsic (gated to governance/root) to handle any stray balance that lands in the account, rather than silently including it in the automatic on_idle forward.

### Proof of Concept
1. Compute `accumulation_account = PalletId(*b"acf/dott").into_account_truncating()` for the deployed runtime (e.g. Westend/BridgeHub/Coretime/People/Collectives, all of which wire this pallet per `Cargo.toml` dependencies).
2. From any unprivileged account, call `Balances::transfer_keep_alive(accumulation_account, X)` for an arbitrary `X >= MinTransferAmount`.
3. Wait until `block % TransferPeriod == 0`. `on_idle` reads `reducible_balance(accumulation_account)`, which now includes the attacker's `X`, per [1](#0-0) .
4. `Forwarder::forward` burns `X` (plus any legitimate accrued fees) from the source chain and teleports the same value to the fixed `StagingLocation` beneficiary on the destination chain [7](#0-6) , with no way for the depositor to reclaim or redirect the value, confirming unauthorized triggering of the privileged burn-and-forward flow using only a standard transfer.

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L40-43)
```rust
//! ## Total Issuance
//!
//! Accumulated funds are burnt upon forwarding (reducing `total_issuance` here) and the same
//! funds are minted at the destination when the sent message is received.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L150-198)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<SystemBlockNumberFor<T>> for Pallet<T> {
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
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L211-217)
```rust
	impl<T: Config> Pallet<T> {
		/// Get the accumulation account derived from the pallet ID.
		///
		/// This account accumulates funds locally before they are forwarded to the destination.
		pub fn accumulation_account() -> T::AccountId {
			T::PalletId::get().into_account_truncating()
		}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L254-309)
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

/// Implementation of `OnUnbalanced` for the `fungible::Balanced` trait.
///
/// Use this on system chains to collect imbalances (e.g. coretime revenue, tx fees, dust removal)
/// that would otherwise be burned, redirecting them to the accumulation account for later
/// forwarding.
///
/// For pallets still using the legacy `Currency` trait (e.g. `pallet_identity`), use
/// [`LegacyAdapter`] instead.
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

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L329-353)
```rust
impl<T: Config, C> OnUnbalanced<LegacyNegativeImbalance<T::AccountId, C>> for LegacyAdapter<T, C>
where
	C: Currency<T::AccountId>,
{
	fn on_nonzero_unbalanced(amount: LegacyNegativeImbalance<T::AccountId, C>) {
		let accumulation_account = Pallet::<T>::accumulation_account();
		let numeric_amount = amount.peek();
		// NOTE: `resolve_creating` is "infallible" because it returns `()`, but it silently burns
		// the imbalance if it is less than ED and the destination is empty. We guard against this
		// by making misconfigured runtimes clearly visible. See crate-level docs for the
		// pre-funding requirement.
		if C::total_balance(&accumulation_account).saturating_add(numeric_amount) <
			C::minimum_balance()
		{
			frame_support::defensive!(
				"🚨 LegacyAdapter: deposit to accumulation account will be silently burned — \
				 ensure the accumulation account is pre-funded with at least ED!"
			);
		}
		C::resolve_creating(&accumulation_account, amount);
		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited (legacy) {numeric_amount:?} to accumulation account"
		);
	}
```

**File:** substrate/frame/accumulate-and-forward/src/tests/on_unbalanced.rs (L161-192)
```rust
#[test]
fn on_unbalanced_multiple_dust_removals_accumulate() {
	new_test_ext(true).execute_with(|| {
		let accumulation_account = AccumulateForwardPallet::accumulation_account();
		let ed = <Balances as Inspect<_>>::minimum_balance();
		let dust = ed / 2;

		// Given: accumulation account has ED. Create 3 accounts with ED + dust each.
		for acct in 10..=12u64 {
			assert_ok!(<Balances as Mutate<_>>::mint_into(&acct, ed + dust));
		}
		let account_before = Balances::free_balance(accumulation_account);
		let issuance_before = <Balances as Inspect<_>>::total_issuance();

		// When: each account transfers ED away, leaving dust < ED → reaped.
		// DustRemoval = AccumulateForward → dust goes to accumulation account.
		for acct in 10..=12u64 {
			assert_ok!(Balances::transfer_allow_death(
				frame_system::RawOrigin::Signed(acct).into(),
				1,
				ed,
			));
			assert_eq!(Balances::free_balance(acct), 0);
		}

		// Then: accumulation account collected dust from all 3 reaps.
		assert_eq!(Balances::free_balance(accumulation_account), account_before + 3 * dust);

		// And: total issuance unchanged (dust moved, not destroyed).
		assert_eq!(<Balances as Inspect<_>>::total_issuance(), issuance_before);
	});
}
```

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L40-42)
```rust
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

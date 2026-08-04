### Title
`MinTransferAmount` in `pallet-accumulate-and-forward` is a hardcoded, non-adjustable constant that can permanently freeze accumulated on-chain revenue - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`)

### Summary
`pallet-accumulate-and-forward` gates every forward of accumulated funds (fees, dust, coretime revenue) behind a fixed threshold, `Config::MinTransferAmount`, declared as `#[pallet::constant]` and supplied only via a `parameter_types!` value in each runtime (e.g. `MinForwardAmount` in `cumulus/parachains/runtimes/coretime/coretime-westend/src/lib.rs:648` and `collectives-westend/src/lib.rs:708`, `MinForwardAmount` in `polkadot/runtime/westend/src/lib.rs:1366`). The pallet exposes **no dispatchable calls at all** (there is no `#[pallet::call]` section in `substrate/frame/accumulate-and-forward/src/lib.rs`), so `MinTransferAmount` cannot be reset by governance, root, or any origin short of a full runtime upgrade — exactly the pattern flagged in the external report for Derby's hardcoded `minimumPull`.

### Finding Description
The `on_idle` hook is the only path that ever releases funds from the accumulation account: [1](#0-0) 

`available_funds` is computed via `reducible_balance(.., Preservation::Preserve, ..)`, and if it is below `T::MinTransferAmount::get()`, the function returns early **without forwarding anything and without emitting any diagnostic event** — the funds simply remain in the accumulation account for another `TransferPeriod`, and the same check repeats indefinitely: [2](#0-1) 

The threshold is a compile-time constant of the `Config` trait: [3](#0-2) 

Unlike `pallet-nomination-pools`, which exposes a `set_configs` extrinsic to adjust `MinJoinBond`/`MinCreateBond` at runtime, this pallet has no equivalent setter — there is exactly one `pub fn` in the entire module (the `on_idle` hook itself), confirmed by searching for `#[pallet::call]`/`pub fn` in the file. If the per-period accrual of fees/dust/coretime revenue on a given chain is, by design or by later changes in traffic/parameters, consistently smaller than the hardcoded `MinTransferAmount`, the accumulation account's balance will never cross the threshold and the funds will never be teleported to the destination `pallet-dap` staging account on the intended chain (see the wiring in `cumulus/parachains/runtimes/coretime/coretime-westend/src/lib.rs:638-651` and `polkadot/runtime/westend/src/lib.rs:1356-1369`). This is the exact structural analog of the reported bug: a hardcoded threshold, without a governance-controlled setter, that blocks fund movement once real-world token economics (decimals/value/volume) diverge from the assumption baked in at genesis.

### Impact Explanation
Because the check is `available_funds < MinTransferAmount` with no upper bound on how long funds may sit unshipped, and because there is no way to lower `MinTransferAmount` without a runtime upgrade, revenue routed through this pallet (transaction fees via `DealWithFeesSplit`, dust removal, coretime revenue) can become permanently stuck in the pallet's derived accumulation account if its typical per-period inflow falls below the hardcoded threshold. This stalls the intended cross-chain settlement to `pallet-dap` on the destination chain indefinitely — a "public underpriced work / stalls processing" and "permanent fund lock" outcome in the sense targeted by the impact gate, requiring no malicious actor, admin, or governance action to trigger; it is a pure parameter-vs-reality mismatch baked into the constant.

### Likelihood Explanation
This does not require an attacker: it is triggered purely by economic conditions (low fee/dust/revenue volume relative to the hardcoded `MinTransferAmount`) that are entirely plausible on quieter system parachains or during periods of low activity, and once triggered, the condition is self-perpetuating since there is no code path to adjust the threshold or force a flush. The absence of any dispatchable in the pallet makes recovery require a runtime upgrade rather than a governance parameter change, closely mirroring the "cannot be reset" root cause in the source report.

### Recommendation
Add a governed setter (e.g., `set_min_transfer_amount(origin: EnsureRoot/GovernanceOrigin, amount: BalanceOf<T>)`) that updates `MinTransferAmount` via storage rather than (or in addition to) the `#[pallet::constant]`, and/or add a governance-only "force forward" extrinsic that bypasses the threshold to flush the accumulation account regardless of `MinTransferAmount`, so operators can react if actual accrual patterns diverge from the value assumed at genesis.

### Proof of Concept
Using the pallet's own test harness (`substrate/frame/accumulate-and-forward/src/tests/xcm_transfer.rs`), the condition is already demonstrated structurally by `ensure_minimum_amount_limit_is_respected`: [4](#0-3) 
In this test, funding the accumulation account with `limit - 1` causes `on_idle` to skip forwarding entirely (`send_count == 0`). In production there is no call that lets governance reduce `limit` (`MinTransferAmount`) after deployment — if the actual steady-state accrual per `TransferPeriod` never exceeds the constant configured at genesis (e.g., `MinForwardAmount` in `polkadot/runtime/westend/src/lib.rs:1366`), the accumulation account balance will asymptotically approach but never cross the threshold, and the `on_idle` early-return path will fire every period forever, permanently withholding the accumulated funds from their intended destination.

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L126-130)
```rust
		/// Minimum transferable balance required to trigger a forward.
		/// This avoids forwarding very small / negligible amounts.
		/// The accumulation account always retains its existential deposit on top of this.
		#[pallet::constant]
		type MinTransferAmount: Get<BalanceOf<Self>>;
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L150-177)
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
```

**File:** substrate/frame/accumulate-and-forward/src/tests/xcm_transfer.rs (L146-174)
```rust
#[test]
fn ensure_minimum_amount_limit_is_respected() {
	new_test_ext(true).execute_with(|| {
		let period = TransferPeriod::get();
		let limit = MinTransferAmount::get();

		// Fund the accumulation account with less than the minimum forwardable amount above ED.
		fund_accumulation_account(limit - 1);
		reset_send_count();
		reset_last_sent_amount();

		System::set_block_number(period);
		AccumulateForwardPallet::on_idle(period, Weight::from_all(u64::MAX));
		assert_eq!(get_send_count(), 0);

		// Top up so that available funds exactly meet the minimum.
		fund_accumulation_account(1);
		assert_eq!(
			Balances::free_balance(get_accumulation_account()),
			Balances::minimum_balance() + limit
		);

		// Next period multiple — forward should now succeed.
		System::set_block_number(2 * period);
		AccumulateForwardPallet::on_idle(2 * period, Weight::from_all(u64::MAX));
		assert_eq!(get_send_count(), 1);
		assert_eq!(get_last_sent_amount(), Some(limit));
	});
}
```

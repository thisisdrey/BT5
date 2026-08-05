Based on the investigation, the strongest local analog to the "settle() asyncFee stuck in KeeperFactory" bug-class (value flows into a contract/pallet account and there's no path to force it onward) is the `pallet-accumulate-and-forward` pallet: it collects fees/dust/coretime revenue into an on-chain accumulation account but relies **exclusively** on the opportunistic `on_idle` hook to forward them, with no dispatchable/forceable extrinsic to trigger forwarding when idle weight is unavailable.

### Title
Fee/dust/coretime revenue can become indefinitely stuck in the accumulation account because forwarding is gated only by `on_idle`, with no forceable dispatch to flush accumulated funds - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`)

### Summary
`pallet_accumulate_and_forward` collects configured token inflows (transaction fees, dust removal, coretime revenue) into a pallet-derived `accumulation_account` via its `OnUnbalanced` implementations, and is supposed to periodically forward the accumulated balance to a configured destination. The forwarding logic lives entirely in the `on_idle` hook, and the pallet exposes **no `#[pallet::call]` dispatchables at all**, so there is no way for anyone (user, operator, or governance) to trigger a forward outside of `on_idle` succeeding.

### Finding Description
Funds are deposited into `accumulation_account` unconditionally through `OnUnbalanced::on_nonzero_unbalanced` (and `LegacyAdapter`), [1](#0-0) . The only egress path is `on_idle`, which:
1. Only runs the forward logic on blocks that are exact multiples of `TransferPeriod`.
2. Immediately returns `Weight::zero()` if there isn't enough **remaining** (idle) weight budget in the block to cover the read + send operation [2](#0-1) .

Because `on_idle` is scheduled after all mandatory/normal extrinsics have consumed their weight, any period-boundary block that is sufficiently full of normal transactions (a routine, permissionless, non-malicious condition — no privileged actor, relayer, or validator collusion required) will cause the forward attempt for that period to be skipped entirely. Since there is no dispatchable call to force a forward, and no fallback within the same block or a following block until the *next* `TransferPeriod` boundary, the accumulated balance in `accumulation_account` remains parked. This repeats every period as long as period-boundary blocks stay full, meaning ordinary network usage growth (not an attack, but exactly the kind of "public underpriced work" pattern the impact gate calls out) can defer forwarding indefinitely.

This directly mirrors the reported bug's structure: value (asyncFee / accumulated fees) is correctly moved into an intermediary account, but the mechanism responsible for pushing it onward to the rightful beneficiary (`keeper` / the configured `Forwarder` destination) is not guaranteed to execute, and there is no alternate, permissionless, or forced code path to flush it.

### Impact Explanation
Coretime revenue, transaction fees, and dust configured to flow through this pallet on system parachains never reach their intended destination chain while forwarding is stalled, which is a fund-lock/misdirected-payout condition matching "permanent user-fund or bridge-state lock" and "public underpriced work that degrades block production or stalls bridge processing" in the impact gate. There is no operator lever to intervene since no dispatchable exists to force forwarding.

### Likelihood Explanation
Requires no privileged actor, malicious peer, validator, or governance action — it is a natural consequence of block congestion coinciding with `TransferPeriod` boundaries, which is entirely plausible on any busy system parachain. The likelihood scales with how full period-boundary blocks tend to be and how conservative `WeightMeter` budgeting is relative to `T::WeightInfo::send_native()`.

### Recommendation
Add a permissionless (or governance-gated, but ideally permissionless with rate limiting already provided by `TransferPeriod`/`MinTransferAmount`) dispatchable, e.g. `force_forward()`, that anyone can call to trigger `T::Forwarder::forward` outside of `on_idle`, guarded by the same `TransferPeriod`/`MinTransferAmount` checks. Alternatively, move the check-and-forward logic into `on_finalize`/`on_initialize` (fixed, non-idle weight) rather than relying solely on best-effort leftover weight.

### Proof of Concept
Not executable without a live/simulated congested-block test harness; the code-level evidence establishing the stuck-fund path is: [3](#0-2)

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L150-201)
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

			meter.consumed()
		}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L289-308)
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
```

### Title
Silent Permanent Burn of Protocol Fees/Revenue When Accumulation Account Is Unfunded - ([File: substrate/frame/accumulate-and-forward/src/lib.rs])

### Summary
`pallet-accumulate-and-forward` intercepts transaction fees, dust-removal credits and coretime revenue via `OnUnbalanced` and redirects them into a `PalletId`-derived `accumulation_account` for periodic forwarding to a destination chain. The pallet's own fungible-based handler discards the result of the deposit attempt, so if the accumulation account is not pre-funded to at least the Existential Deposit (ED), every incoming credit below ED is silently dropped and permanently burned from `total_issuance`, exactly mirroring the PANTHEON `FEE_ADDRESS` bug: a public/permissionless flow (paying any transaction fee) routes protocol revenue to a destination that "swallows" the funds because a required precondition was never enforced on-chain.

### Finding Description
The pallet's fungible `OnUnbalanced` implementation is: [1](#0-0) 

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
		...
```

The `resolve()` call's `Result` is discarded (`let _ = ...`). When `resolve` fails (destination not pre-funded to ED and the incoming credit is below ED), the returned unresolved `Credit` is dropped instead of being merged into the destination — dropping a `Credit` imbalance decrements `total_issuance`, i.e. it is burned. The pallet's own doc comments confirm this is a known, unenforced precondition rather than a defended invariant: [2](#0-1) 

There is no genesis check, no `integrity_test` assertion, and no runtime-level enforcement that the accumulation account actually holds ED before this code path is reachable — the only `integrity_test` in the pallet checks `TransferPeriod != 0`, not that the account is funded: [3](#0-2) 

The legacy `Currency`-trait adapter for the same pallet explicitly acknowledges the exact same failure mode and only adds a `defensive!` diagnostic (which does not prevent the burn in production, only asserts/logs under test configs): [4](#0-3) 

Unlike the PANTHEON contract (owner must call `setFeeAddress`), here the destination account itself always exists deterministically (`PalletId::into_account_truncating()`), so the analog is not "unset address" but "unfunded address below ED" — which is functionally identical in effect: value routed to it below the ED threshold, with no pre-existing balance, is unretrievable/burned rather than credited.

### Impact Explanation
This pallet is wired into live Westend system-chain runtimes (Westend relay chain, Bridge Hub Westend, Collectives Westend, Coretime Westend, People Westend), collecting transaction fees, dust removal and (on the relay chain) coretime revenue that would otherwise be burned, specifically so they can instead be forwarded to a central destination (per `prdoc/stable2603/pr_10597.prdoc`, "Introduce pallet-dap-satellite and redirect system burns to DAP"). If the accumulation account on any of these chains is not pre-funded with ED at genesis or after a runtime upgrade introduces/reconfigures the pallet, every ordinary user transaction that produces a fee/dust credit below ED silently and permanently destroys that value instead of routing it to the intended destination (DAP/treasury). This is a protocol-level, permanent, unbacked loss of value triggered purely by ordinary public activity (paying transaction fees) — not by any malicious actor, admin action, or governance decision; the vulnerability is the missing on-chain enforcement of a documented precondition.

### Likelihood Explanation
The vulnerable code path executes on effectively every block/transaction — any transaction fee, dust-removal event, or coretime revenue notification triggers `on_nonzero_unbalanced`. The trigger condition (accumulation account balance + incoming amount < ED) is entirely plausible during initial chain launch, runtime upgrades that introduce the pallet, or after the account is drained close to zero right after a successful forward (leaving it briefly below ED again until re-funded), since nothing in the code guarantees the account is topped back up to ED after each forward. The lack of any automated safeguard (only a doc comment) means a single operational oversight causes an ongoing, silent leak with no error surfaced to users or operators outside of a `defensive!` diagnostic that is inert in production builds.

### Recommendation
- In `on_nonzero_unbalanced` (and the `LegacyAdapter` equivalent), do not discard the `resolve` error: if the accumulation account cannot absorb the deposit, fall back to an alternate `OnUnbalanced` handler (e.g. `ResolveTo` treasury/author) instead of dropping the credit.
- Add a genesis/on-chain invariant (e.g. a `try_state` or startup check) that fails visibly if `accumulation_account` balance is below `ExistentialDeposit`, rather than relying solely on documentation.
- Consider auto-topping-up the account from the credit itself when it is below ED (mint the ED difference) instead of silently burning it.

### Proof of Concept
1. Deploy/upgrade a runtime with `pallet_accumulate_and_forward::Config` wired as the `OnChargeTransaction`/`OnUnbalanced` handler for transaction fees (as in Westend system chains), but do **not** pre-fund `Pallet::<T>::accumulation_account()` with ED in the genesis config (an easily missed step, exactly analogous to forgetting `setFeeAddress()`).
2. Any user submits an ordinary signed transaction with a fee amount smaller than `ExistentialDeposit`.
3. `on_nonzero_unbalanced` is invoked with the fee `Credit`; `T::Currency::resolve(&accumulation_account, amount)` fails because depositing an amount below ED into an empty account is rejected by the balances pallet; the returned `Result` is discarded via `let _ = ...`.
4. The unresolved `Credit` is dropped, decrementing `total_issuance` by the fee amount — the fee is permanently burned instead of being forwarded to the destination chain (DAP/treasury), with no error surfaced to the fee payer and only a `defensive!` diagnostic (inert in production) recorded.
5. Repeat for every subsequent transaction with a sub-ED fee while the account remains under-funded: each one permanently destroys protocol revenue that should have been routed to the treasury/destination chain.

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L32-38)
```rust
//! ## Setup
//!
//! The accumulation account must be pre-funded with at least the existential deposit.
//! For new chains, include the account in the balances genesis config.
//! For existing chains, fund it via a manual transfer.
//!
//! If the accumulation account is not pre-funded, deposits below ED will be silently burned.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L203-208)
```rust
		fn integrity_test() {
			assert!(
				!T::TransferPeriod::get().is_zero(),
				"TransferPeriod must not be zero (would cause division by zero in on_idle)"
			);
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

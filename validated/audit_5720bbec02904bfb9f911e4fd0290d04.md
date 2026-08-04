Confirmed: there is no `PGasDepositOf` (per-depositor PGAS entitlement map) analogous to `NativeDepositOf<T>`. This confirms the structural gap in `substrate/frame/revive/src/deposit_payment.rs`.

### Title
PGAS storage-deposit refund settles against the contract's pooled PGAS hold instead of the specific depositor's entitlement, letting one depositor's refund burn/consume another depositor's PGAS deposit - (File: `substrate/frame/revive/src/deposit_payment.rs`)

### Summary
`PGasDeposit::refund_on_hold` and `settle_pgas_refund` cap the PGAS portion of a storage-deposit refund by the *contract's total* PGAS balance on hold (`pgas_on_hold(reason, from)` where `from` is the contract), not by how much PGAS the specific refund recipient (`to`) actually contributed. Unlike the native-currency path, which correctly tracks and caps per-depositor entitlement via `NativeDepositOf<T>`, there is no equivalent per-depositor bookkeeping for the PGAS side. This is structurally the same bug class as the Flan `safeTransfer()` issue: an accounting check performed against a shared/pooled balance (`address(this)` / the contract's total hold) instead of the balance actually attributable to the counterparty who should be paid or charged.

### Finding Description
In `refund_on_hold` (`substrate/frame/revive/src/deposit_payment.rs:384-412`): [1](#0-0) 
the native portion is correctly capped via `NativeDepositOf::<T>::get(from, to)` — a double map keyed `(contract, depositor) -> amount`, so a refund to depositor `to` can never draw down more native currency than `to` personally contributed to that contract's hold.

However, the PGAS shortfall (`pgas_needed`) is settled via `settle_pgas_refund`: [2](#0-1) 
which caps `amount` at `Self::pgas_on_hold(reason, from)` — the **total** PGAS the contract (`from`) has on hold under `HoldReason::StorageDepositReserve`, aggregated across *all* depositors who ever paid PGAS into that contract's storage deposit. There is no `PGasDepositOf`-style map to attribute this pooled hold to the specific depositor being refunded. The map used for the analogous native check (`NativeDepositOf`, confirmed via repo grep to have no PGAS counterpart) exists only for native currency, confirming the asymmetry is real and not just an oversight in my reading.

As a result, when a contract has multiple contributors to its PGAS storage deposit (exactly the scenario exercised by the `MultiContributorStorage` test fixture and `destroy_contract_reaps_account_and_clears_native_deposit_map` test), a refund triggered on behalf of depositor A can draw from and burn/partially-refund PGAS that was actually contributed by depositor B, because `settle_pgas_refund` only checks "does the contract have enough PGAS on hold in aggregate," not "does *this* depositor have that much of *their own* PGAS on hold."

### Impact Explanation
This breaks the "conserve value, settle exactly once to the rightful beneficiary" invariant required by the impact gate for balances/contract-held value. Concretely:
- Depositor B's PGAS storage deposit can be silently reduced (partially refunded to A's free balance at `RefundPercent`, and majority burned) when A's unrelated storage shrinks, even though A never contributed that PGAS.
- This is triggerable by any two unprivileged users interacting normally with the same contract (e.g., growing/shrinking storage) — no malicious relayer, validator, governance actor, or leaked key is required, satisfying the "unprivileged attacker" requirement of the pivots.
- The effect is a fund-loss/misattribution bug: depositor B eventually cannot recover their full PGAS deposit on their own storage removal because it was already consumed refunding A.

### Likelihood Explanation
Requires only: (1) a contract configured with `Config::Deposit = PGasDeposit<...>` (already shipped per `prdoc/stable2606/pr_11847.prdoc`), and (2) two or more distinct accounts each contributing PGAS-backed storage deposits to the same contract, followed by a refund-triggering operation (e.g. storage shrink, contract termination path via `refund_all`, or `check_payment`-style retry flows). This is a normal multi-user contract usage pattern (the repo's own `MultiContributorStorage` fixture exists specifically to test multi-depositor scenarios), so likelihood is not a contrived edge case — it's the expected operating mode of any shared/popular contract on PGAS-enabled chains.

### Recommendation
Introduce a per-depositor PGAS entitlement map analogous to `NativeDepositOf<T>` (e.g. `PGasDepositOf<T>: StorageDoubleMap<contract, depositor, BalanceOf<T>>`), populated in the PGAS branch of `charge_and_hold` (mirroring `record_native_deposit`), and have `settle_pgas_refund` cap `amount` by `min(pgas_needed, PGasDepositOf::get(from, to))` instead of the contract's total `pgas_on_hold`. Reserve the uncapped, total-pool draw only for `refund_all` at contract termination, where there is a single terminal beneficiary and pooling is acceptable.

### Proof of Concept
Conceptual reproduction (extending the existing `mixed_native_pgas_refund_caps_pgas_without_reverting`-style harness in `substrate/frame/revive/src/tests/deposit_payment.rs`):
1. Set up a contract; ALICE pays a PGAS-backed storage charge of 100 (contract PGAS hold = 100, no `PGasDepositOf` entry exists to record this is ALICE's).
2. CHARLIE separately pays a PGAS-backed storage charge of 100 on the same contract (contract PGAS hold = 200).
3. Trigger a refund for CHARLIE alone requesting 150 (exceeding CHARLIE's own 100 contribution).
4. Because `settle_pgas_refund` only checks the contract's aggregate `pgas_on_hold` (200 ≥ 150), the refund proceeds using PGAS that includes 50 units contributed by ALICE, burning/reallocating funds that were never CHARLIE's — with no error and no on-chain accounting distinguishing whose PGAS was actually spent, unlike the equivalent native-refund test which is capped exactly by `NativeDepositOf`. [3](#0-2)

### Citations

**File:** substrate/frame/revive/src/deposit_payment.rs (L393-410)
```rust
		let contribution = NativeDepositOf::<T>::get(from, to);
		let native_requested = amount.min(contribution);

		let native_refunded = if !native_requested.is_zero() {
			<() as Deposit<T>>::refund_on_hold(reason, from, dst, native_requested)?;
			let new_val = contribution.saturating_sub(native_requested);
			if new_val.is_zero() {
				NativeDepositOf::<T>::remove(from, to);
			} else {
				NativeDepositOf::<T>::insert(from, to, new_val);
			}
			native_requested
		} else {
			BalanceOf::<T>::zero()
		};

		let pgas_needed = amount.saturating_sub(native_refunded);
		Self::settle_pgas_refund(reason, from, to, pgas_needed)?;
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L575-591)
```rust
	fn settle_pgas_refund(
		reason: HoldReason,
		from: &T::AccountId,
		to: &T::AccountId,
		amount: BalanceOf<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		if amount.is_zero() {
			return Ok(BalanceOf::<T>::zero());
		}
		// Cap the amount we settle at what's actually held in PGAS. A refund recipient with
		// no `NativeDepositOf` credit on a contract whose deposit was paid in native would
		// otherwise route the full amount through PGAS and revert on `Precision::Exact`.
		let pgas_held = Self::pgas_on_hold(reason, from);
		let amount = amount.min(pgas_held);
		if amount.is_zero() {
			return Ok(BalanceOf::<T>::zero());
		}
```

**File:** substrate/frame/revive/src/tests/deposit_payment.rs (L620-673)
```rust
fn mixed_native_pgas_refund_caps_pgas_without_reverting() {
	run(TestCase {
		accounts: vec![
			AccountSetup { account: ALICE, native: 1_000, pgas: 0 },
			AccountSetup { account: CHARLIE, native: 1_000, pgas: 1_000 },
		],
		charges: vec![
			Charge {
				payer: ALICE,
				amount: 100,
				expected: State {
					payer_native: 900,
					contract_native_held: 100,
					native_entitlement: 100,
					..State::default()
				},
			},
			Charge {
				payer: CHARLIE,
				amount: 40,
				expected: State {
					payer_native: 1_000,
					payer_pgas: 960,
					contract_native_held: 100,
					contract_pgas_held: 40,
					..State::default()
				},
			},
		],
		refund: (CHARLIE, 80),
		expected_after_refund: vec![
			(
				ALICE,
				State {
					payer_native: 900,
					contract_native_held: 100,
					native_entitlement: 100,
					..State::default()
				},
			),
			(
				CHARLIE,
				State {
					payer_native: 1_000,
					// CHARLIE pays 40 PGAS, then receives a 10% refund on the capped 40 PGAS
					// settlement: 1_000 - 40 + 4.
					payer_pgas: 964,
					contract_native_held: 100,
					..State::default()
				},
			),
		],
	});
}
```

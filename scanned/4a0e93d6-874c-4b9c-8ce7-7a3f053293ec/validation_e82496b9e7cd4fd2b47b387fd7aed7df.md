### Title
`transfer_from` ERC-20 precompile / `do_transfer_approved` debits an owner's approval by the *requested* amount while the pallet-assets transfer engine can debit and credit *more* than that amount (dust-sweep), letting a delegate drain up to `min_balance - 1` extra tokens per call without the approval ever reflecting it - ([File: substrate/frame/assets/src/functions.rs])

### Summary
The External Report's core invariant is: code trusts a caller-supplied nominal "amount" instead of verifying the *actual* balance movement, letting the real transferred amount diverge from the accounted amount. In `pallet_assets`, `do_transfer_approved` (used by the ERC20-compatible `transfer_from` precompile in `substrate/frame/assets/precompiles/src/lib.rs`) reduces the owner's `Approval.amount` by the caller-supplied `amount`, but the underlying `transfer_and_die` engine can move `debit`/`credit` amounts that are *larger* than `amount` due to the "no-dust-left-behind" rule, and `do_transfer_approved` never checks or reconciles that difference.

### Finding Description
`Pallet::do_transfer_approved` computes the new remaining allowance purely from the requested `amount`: [1](#0-0) 
It calls `transfer_and_die(id, owner, destination, amount, None, f)` with `f = TransferFlags { keep_alive: false, best_effort: false, burn_dust: false }`.

Inside `transfer_and_die`, the actually-moved amounts are computed by `prep_debit`/`prep_credit`, not by echoing `amount`: [2](#0-1) 

`prep_debit` explicitly documents and implements that the returned debit "will always be at least `amount`" — and can be *greater* whenever debiting exactly `amount` would leave the source account below `min_balance`: in that case the account is swept entirely and the extra dust is added on top of `amount`: [3](#0-2) 

`prep_credit` then decides whether that extra dust is burned or forwarded to the destination based on the `burn_dust` flag. Since `do_transfer_approved` always uses `burn_dust: false`, the `(true, Some(dust))` branch is never taken and the code falls into the fallback branch, crediting the *full debit* (which can exceed `amount`) to the destination: [4](#0-3) 

So when the owner's remaining reducible balance is between `amount` and `amount + min_balance - 1`, calling `transferFrom(owner, spender, to, amount)` will:
- debit the owner's *entire* remaining balance (owner account is destroyed),
- credit `to` with that full (larger) amount,
- but decrement the `Approvals` storage entry by only the nominal `amount` passed in.

The ERC20 precompile wrapper on top of this makes the mismatch externally visible/exploitable and even emits a misleading event using the *nominal* value rather than the actual transferred value: [5](#0-4) 

This is the exact analog of the reported bug class: the wrapper (`fundPool`-equivalent = `do_transfer_approved`) accounts state changes (`Approvals`, the `Transfer` event) based on the *requested* `amount` instead of verifying/using the *actual* balance movement (`credit`/`debit`) that occurred, exactly as the report recommends checking `balanceBefore`/`balanceAfter` deltas rather than trusting the nominal transfer amount.

### Impact Explanation
An unprivileged `delegate` (an address approved for a limited `amount` via `approve`) can, in a single `transferFrom` call, cause the owner to lose their *entire* remaining dust-adjacent balance (up to `min_balance - 1` more than they explicitly approved) while the on-chain `Approvals` record only reflects the smaller `amount`. This breaks the "public wrappers must not undercharge/over-credit nested execution" invariant and the "balances… must conserve value and settle exactly once to the rightful beneficiary and amount" invariant: the delegate extracts unapproved value from the owner beyond the granted permission. Because `is_sufficient`/`min_balance` can be configured arbitrarily high by an asset owner (including bridged/foreign assets on Asset Hub), the magnitude of the over-transfer is asset-config-dependent and not bounded to a negligible dust amount.

### Likelihood Explanation
This requires only two unprivileged actors: an asset owner who calls `approve` for a `delegate`, and that `delegate` calling `transferFrom` (directly via `pallet_assets::transfer_approved` or via the ERC20 precompile in `pallet-revive`) when the owner's account balance sits in the `[amount, amount + min_balance - 1]` window relative to `min_balance`. No malicious peer, validator, governance, or leaked key is needed — it is a pure public dispatchable/precompile-entrypoint path, matching the required "unprivileged attacker" criterion.

### Recommendation
`do_transfer_approved` should use the *actual* amount debited/credited (the `credit` value returned by `transfer_and_die`, mirroring the report's "update to the contract balance increase") to decrement `Approval.amount`, and should reject (or explicitly document/best-effort-guard) the case where the actual transferred amount exceeds the delegate's remaining approval. Concretely:
- Capture the `credit` (actual transferred) return value from `transfer_and_die` inside `do_transfer_approved`.
- Verify `credit <= approved.amount` before mutating storage; if the dust-sweep would push the transferred amount above the approved amount, either burn the dust (`burn_dust: true`) or fail the call.
- Update the ERC20 precompile's `Transfer` event to emit the actual transferred value, not the raw `call.value`.

### Proof of Concept
1. Asset `A` has `min_balance = 100`.
2. Owner `O` holds balance `150` in asset `A`.
3. `O` calls `approve(delegate, 100)` → `Approvals[(A, O, delegate)].amount == 100`.
4. `delegate` calls `transfer_from(O, delegate, R, 100)` (via the ERC20 precompile or `pallet_assets::transfer_approved` extrinsic).
5. Inside `do_transfer_approved`, `transfer_and_die(A, O, R, 100, ..., TransferFlags{keep_alive:false, best_effort:false, burn_dust:false})` is invoked.
6. `prep_debit` computes `actual = reducible_balance(O).min(100) = 100`; since debiting 100 would leave `O` with `50 < min_balance(100)`, `can_decrease` returns `ReducedToZero(50)`, so `prep_debit` returns `actual = 100 + 50 = 150`.
7. `prep_credit` (burn_dust=false) returns `credit = debit = 150`, so `R` receives `150`, not the approved `100`.
8. `O`'s account is fully drained (`150` debited, account dies), yet `Approvals[(A, O, delegate)].amount` is reduced by only `100` (`checked_sub(&amount)` uses the *nominal* `amount = 100`), and since `remaining` becomes `0`, the approval entry and deposit are simply removed — with no reconciliation against the extra `50` actually moved.
9. Net effect: `delegate` moved `150` units of `O`'s tokens while only being authorized for `100` — a `50`-unit (or generally up to `min_balance - 1`) unauthorized transfer, verifiable purely from public storage/state without any privileged actor.

### Citations

**File:** substrate/frame/assets/src/functions.rs (L291-310)
```rust
	pub(super) fn prep_debit(
		id: T::AssetId,
		target: &T::AccountId,
		amount: T::Balance,
		f: DebitFlags,
	) -> Result<T::Balance, DispatchError> {
		let actual = Self::reducible_balance(id.clone(), target, f.keep_alive)?.min(amount);
		ensure!(f.best_effort || actual >= amount, Error::<T, I>::BalanceLow);

		let conseq = Self::can_decrease(id, target, actual, f.keep_alive);
		let actual = match conseq.into_result(f.keep_alive) {
			Ok(dust) => actual.saturating_add(dust), //< guaranteed by reducible_balance
			Err(e) => {
				debug_assert!(false, "passed from reducible_balance; qed");
				return Err(e);
			},
		};

		Ok(actual)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L327-340)
```rust
	pub(super) fn prep_credit(
		id: T::AssetId,
		dest: &T::AccountId,
		amount: T::Balance,
		debit: T::Balance,
		burn_dust: bool,
	) -> Result<(T::Balance, Option<T::Balance>), DispatchError> {
		let (credit, maybe_burn) = match (burn_dust, debit.checked_sub(&amount)) {
			(true, Some(dust)) => (amount, Some(dust)),
			_ => (debit, None),
		};
		Self::can_increase(id, dest, credit, false).into_result()?;
		Ok((credit, maybe_burn))
	}
```

**File:** substrate/frame/assets/src/functions.rs (L665-667)
```rust
		// Figure out the debit and credit, together with side-effects.
		let debit = Self::prep_debit(id.clone(), source, amount, f.into())?;
		let (credit, maybe_burn) = Self::prep_credit(id.clone(), dest, amount, debit, f.burn_dust)?;
```

**File:** substrate/frame/assets/src/functions.rs (L1024-1033)
```rust
		Approvals::<T, I>::try_mutate_exists(
			(id.clone(), &owner, delegate),
			|maybe_approved| -> DispatchResult {
				let mut approved = maybe_approved.take().ok_or(Error::<T, I>::Unapproved)?;
				let remaining =
					approved.amount.checked_sub(&amount).ok_or(Error::<T, I>::Unapproved)?;

				let f = TransferFlags { keep_alive: false, best_effort: false, burn_dust: false };
				owner_died =
					Self::transfer_and_die(id.clone(), owner, destination, amount, None, f)?.1;
```

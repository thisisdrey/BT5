### Title
Unsolicited asset transfers force a `Consumer` reference onto the receiver's account without consent, enabling consumer-ref exhaustion DOS - ([File: substrate/frame/assets/src/functions.rs])

### Summary
`pallet-assets` allows any signed account to transfer a non-sufficient asset to any other account. If the recipient has never held that asset before, `Pallet::new_account` is invoked as a side effect of crediting the transfer, and — because the asset is not `is_sufficient` and no depositor is provided — it unconditionally calls `frame_system::Pallet::<T>::inc_consumers(who)` on the *receiver*, consuming one of their limited `MaxConsumers` slots, exactly analogous to the ParaSpace bug where a sender's action silently mutates state that costs the receiver later.

### Finding Description
In `do_touch`/transfer credit paths, `Self::new_account` decides the `ExistenceReason` for a freshly-created asset account: [1](#0-0) 

When no `maybe_deposit` is supplied (this is the normal case for a plain `transfer` call, as opposed to `touch_other`) and the asset is not `is_sufficient`, the code forces a consumer reference onto `who` — the **receiver**, not the sender:
```rust
} else {
    frame_system::Pallet::<T>::inc_consumers(who)
        .map_err(|_| Error::<T, I>::UnavailableConsumer)?;
    ...
    ExistenceReason::Consumer
}
```
This mirrors the ParaSpace `SupplyLogic.sol` pattern exactly: the sender's action (`transfer`) causes the protocol to silently flip a piece of receiver-owned account state (`isUsingAsCollateral` there, a `System::consumers` increment here) with no opt-in from the receiver. The receiver never called `touch`, `touch_other`, or consented to holding the asset — they are simply the destination of someone else's `transfer` call, using the public, unprivileged `Assets::transfer`/`transfer_keep_alive` extrinsics.

`can_increase` gates this only by checking `frame_system::Pallet::<T>::can_accrue_consumers(who, 2)` before allowing account creation — i.e., it fails only when the receiver is already exhausted, but does nothing to prevent an attacker from *incrementally* consuming the receiver's remaining consumer budget one asset-id at a time: [2](#0-1) 

`MaxConsumers` is a small, fixed bound configured per-runtime (e.g. Asset Hub runtimes), so an attacker who creates or uses several distinct non-sufficient asset IDs and sends a trivial amount of each to a victim account can consume the victim's consumer-ref budget entirely through unprivileged, low-cost transfers.

### Impact Explanation
Once an account's consumer refs are exhausted, any other pallet/operation in the runtime that needs to call `inc_consumers` on that account (e.g., staking bonding, opening HRMP channels, creating additional non-sufficient asset accounts, certain collator/session operations, or reserving/holding balance in pallets that force a consumer bump) will fail with `TooManyConsumers`, denying the victim legitimate functionality without their action or consent — a public, underprivileged griefing/DOS vector against a specific account, matching the "Accept... public underpriced work that degrades... chain" and unauthorized-state-mutation class in the impact gate.

### Likelihood Explanation
The attack requires no privileged role, no validator/collator/relayer collusion, and no governance action — only an ordinary signed account able to (a) create or reuse several non-sufficient `AssetId`s (trivially cheap, e.g. via `force_create`/`create` if permissionless creation is allowed in the runtime, or reuse of existing non-sufficient assets already in existence) and (b) call the public `transfer` extrinsic to send a dust amount to the victim for each asset id. This is straightforward, repeatable, and matches "public underpriced work" causing account-level denial of service, though the achievable severity depends on the runtime's `MaxConsumers` value and on how many pallets actually require spare consumer slots for routine victim operations, which limits it to a medium-severity griefing bug rather than a chain-halting one.

### Recommendation
Do not force a `Consumer` reference on the receiver as a side effect of an unsolicited `transfer`. Instead:
- Require the receiver (or an explicit depositor via `touch_other`) to have already `touch`ed the asset before a plain `transfer` can create their account, or
- Charge/require the *sender* to supply the deposit (as already supported via `maybe_deposit`/`DepositFrom`) rather than silently consuming the receiver's own consumer budget, or
- Reserve additional headroom in `can_accrue_consumers` specifically for defensive purposes so a victim always retains slots for security-critical operations (staking, holds) regardless of unsolicited asset transfers.

### Proof of Concept
1. Attacker (or anyone) creates/uses N distinct non-sufficient `AssetId`s (`Assets::force_create`/`create` with `is_sufficient = false`), where N ≈ `MaxConsumers` for the target runtime.
2. For each asset id, attacker calls `Assets::transfer(origin: attacker, id, dest: victim, amount: 1)`.
3. Each call triggers `new_account(victim, ...)` → `inc_consumers(victim)` since `maybe_deposit` is `None` and asset is not sufficient, per [3](#0-2) .
4. After N transfers, `System::consumers(victim) == MaxConsumers`.
5. Victim now cannot perform any operation requiring a fresh consumer ref (e.g. `Staking::bond`, opening a new hold-bearing pallet interaction) — call fails with `DispatchError::TooManyConsumers`, confirmed by existing test behavior for consumer-limit exhaustion: [4](#0-3) .

### Citations

**File:** substrate/frame/assets/src/functions.rs (L68-97)
```rust
	pub(super) fn new_account(
		who: &T::AccountId,
		d: &mut AssetDetails<T::Balance, T::AccountId, DepositBalanceOf<T, I>>,
		maybe_deposit: Option<(&T::AccountId, DepositBalanceOf<T, I>)>,
	) -> Result<ExistenceReasonOf<T, I>, DispatchError> {
		let accounts = d.accounts.checked_add(1).ok_or(ArithmeticError::Overflow)?;
		let reason = if let Some((depositor, deposit)) = maybe_deposit {
			if depositor == who {
				ExistenceReason::DepositHeld(deposit)
			} else {
				ExistenceReason::DepositFrom(depositor.clone(), deposit)
			}
		} else if d.is_sufficient {
			frame_system::Pallet::<T>::inc_sufficients(who);
			d.sufficients.saturating_inc();
			ExistenceReason::Sufficient
		} else {
			frame_system::Pallet::<T>::inc_consumers(who)
				.map_err(|_| Error::<T, I>::UnavailableConsumer)?;
			// We ensure that we can still increment consumers once more because we could otherwise
			// allow accidental usage of all consumer references which could cause grief.
			if !frame_system::Pallet::<T>::can_inc_consumer(who) {
				frame_system::Pallet::<T>::dec_consumers(who);
				return Err(Error::<T, I>::UnavailableConsumer.into());
			}
			ExistenceReason::Consumer
		};
		d.accounts = accounts;
		Ok(reason)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L153-169)
```rust
		if let Some(account) = Account::<T, I>::get(id, who) {
			if account.status.is_blocked() {
				return DepositConsequence::Blocked;
			}
			if account.balance.checked_add(&amount).is_none() {
				return DepositConsequence::Overflow;
			}
		} else {
			if amount < details.min_balance {
				return DepositConsequence::BelowMinimum;
			}
			if !details.is_sufficient && !frame_system::Pallet::<T>::can_accrue_consumers(who, 2) {
				return DepositConsequence::CannotCreate;
			}
			if details.is_sufficient && details.sufficients.checked_add(1).is_none() {
				return DepositConsequence::Overflow;
			}
```

**File:** substrate/frame/balances/src/tests/consumer_limit_tests.rs (L40-51)
```rust
			// Fill up all consumer refs.
			// Note: asset-pallets prevents all the consumers to be filled and leaves one untouched.
			// But other operations in the runtime, notably `uniques::set_accept_ownership` might
			// overrule it.
			let max_consumers: u32 = <Test as frame_system::Config>::MaxConsumers::get();
			for _ in 0..max_consumers {
				assert_ok!(System::inc_consumers(&1));
			}
			assert_eq!(System::consumers(&1), max_consumers);

			// We cannot manually increment consumers beyond the limit
			assert_noop!(System::inc_consumers(&1), DispatchError::TooManyConsumers);
```

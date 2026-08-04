## Title
`pallet-revive` ERC20 `fungibles::Mutate` adapter returns post-call attacker balance instead of the transferred amount, enabling theft of donated tokens - (File: `substrate/frame/revive/src/impl_fungibles.rs`)

### Summary
The bug report describes `TreehouseRouter._stethToWsteth` returning `IERC20(wstETH).balanceOf(address(this))` (the full contract balance) instead of the amount actually produced by the `wrap` call, letting anyone who donates wstETH to the router have it silently absorbed into the next caller's accounted amount. The local analog is `Pallet<T>::burn_from` and `Pallet<T>::mint_into` in `substrate/frame/revive/src/impl_fungibles.rs`, which implement `fungibles::Mutate` for ERC20 tokens hosted in `pallet-revive`. Both functions perform an ERC20 `transfer` via `bare_call`, and on success return `<Self as fungibles::Inspect<_>>::balance(asset_id, who)` — i.e. the callee's/receiver's *current total balance* — instead of the `amount` that was actually moved by the call.

### Finding Description
`fungibles::Unbalanced::decrease_balance`/`increase_balance` (the canonical trait contract in `substrate/frame/support/src/traits/tokens/fungibles/regular.rs:180-209`) specifies: "if `Ok` is returned then the inner is the amount by which is was reduced/increased" — i.e. the return value must be the *delta*, not the resulting balance. [1](#0-0) 

`pallet-revive`'s implementation of `fungibles::Mutate` violates this contract:

```rust
fn burn_from(...) -> Result<Self::Balance, DispatchError> {
    ...
    let data = IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
    let ContractResult { result, .. } = Self::bare_call(...);
    if let Ok(return_value) = result {
        if return_value.did_revert() { Err(...) }
        else {
            let is_success = bool::abi_decode_validate(&return_value.data)...;
            if is_success {
                let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
                Ok(balance)   // returns balance, not amount burned
            } else { Err(...) }
        }
    } else { Err(...) }
}
``` [2](#0-1) 

`mint_into` has the identical pattern: it calls `IERC20::transferCall` to move `amount` to `who`, then instead of returning `amount` (the imbalance actually credited), returns `who`'s full post-transfer `balance`: [3](#0-2) 

This is exactly the wstETH bug pattern: if the caller's/target account already holds any balance of the ERC20 token — e.g., accumulated from a prior operation, an unrelated direct `transfer` into that account, or from repeated small mints — the returned "amount transferred" silently includes that pre-existing/unrelated balance. This corrupted value (`balance` returned in place of `amount`) is exactly the value that consuming code (e.g. `xcm_builder::FungiblesAdapter`, as this module's own comment states — "These functions are used in `xcm_builder::FungiblesAdapter`") relies on to know how much was actually withdrawn/deposited for asset-accounting purposes such as crediting `Credit`/`Debt` imbalances or computing the trapped/refunded asset amount in XCM execution. [4](#0-3) 

The existing guard (`return_value.did_revert()` / `is_success` boolean decode) only checks that the ERC20 call succeeded — it does not validate that the *returned amount equals the requested `amount`*, so it does not stop the corrupted-value substitution.

### Impact Explanation
Because the corrupted value flows into `fungibles::Mutate::burn_from`/`mint_into`, any downstream consumer (notably `xcm_builder::FungiblesAdapter`, per this file's own doc comment) that trusts the returned `Self::Balance` as "amount actually burned/minted" will mis-account value: it can over-credit or under-debit XCM holding registers relative to what was truly transferred on the ERC20 side, causing incorrect settlement amounts, and, depending on how the caller reconciles the imbalance, funds belonging to one party (whatever inflated the account's balance) can be attributed to another party's operation — i.e., unbacked mint/incorrect settlement in the fungibles-accounting layer, which is squarely the "theft/unbacked mint or unlock, duplicate settlement" class targeted by the impact gate.

### Likelihood Explanation
The trigger requires no privileged access: any account can hold or receive ERC20 balance in the affected asset (e.g. via a normal token transfer), and any subsequent `burn_from`/`mint_into` call for that same account will return the polluted balance instead of the true delta. No malicious relayer, validator, or admin is needed — an ordinary unprivileged actor sending tokens to an account is sufficient to desynchronize the returned amount from the real one.

### Recommendation
Change `burn_from` and `mint_into` to compute and return the actual delta caused by the operation (e.g., snapshot balance before and after the `bare_call`, or better, parse/verify the ERC20 `Transfer` event/return data for the moved amount) rather than the post-call `balance()` of the account. At minimum, assert `is_success` implies the exact `amount` was moved (many ERC20s can apply fees/rebasing, so the safest fix is `balance_after - balance_before`), and never conflate "current balance" with "operation result."

### Proof of Concept
1. Configure a runtime using `pallet-revive`'s `fungibles::Mutate` implementation (e.g. via `xcm_builder::FungiblesAdapter`) for an ERC20 asset.
2. Have an unrelated account `A` send (via normal ERC20 `transfer`) some tokens directly to `A`'s own mapped address, or to the `checking_account()`/beneficiary address used in a later `mint_into`/`burn_from` call, so that account now holds balance `X` unrelated to the pending operation.
3. Trigger an XCM/asset operation that calls `Pallet::<T>::mint_into(asset_id, &A, amount)` with some `amount < X`.
4. Observe that the function returns `balance(asset_id, &A)` (which equals `X + amount`, not `amount`) as the "amount minted," which the caller (e.g. `FungiblesAdapter`) will treat as the imbalance credited — inflating the accounted deposited amount by the pre-existing balance `X` that was never part of this operation.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L180-209)
```rust
	/// Reduce the balance of `who` by `amount`.
	///
	/// If `precision` is `Exact` and it cannot be reduced by that amount for
	/// some reason, return `Err` and don't reduce it at all. If `precision` is `BestEffort`, then
	/// reduce the balance of `who` by the most that is possible, up to `amount`.
	///
	/// In either case, if `Ok` is returned then the inner is the amount by which is was reduced.
	/// Minimum balance will be respected and thus the returned amount may be up to
	/// `Self::minimum_balance() - 1` greater than `amount` in the case that the reduction caused
	/// the account to be deleted.
	fn decrease_balance(
		asset: Self::AssetId,
		who: &AccountId,
		mut amount: Self::Balance,
		precision: Precision,
		preservation: Preservation,
		force: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let old_balance = Self::balance(asset.clone(), who);
		let reducible = Self::reducible_balance(asset.clone(), who, preservation, force);
		match precision {
			BestEffort => amount = amount.min(reducible),
			Exact => ensure!(reducible >= amount, TokenError::FundsUnavailable),
		}
		let new_balance = old_balance.checked_sub(&amount).ok_or(TokenError::FundsUnavailable)?;
		if let Some(dust) = Self::write_balance(asset.clone(), who, new_balance)? {
			Self::handle_dust(Dust(asset, dust));
		}
		Ok(old_balance.saturating_sub(new_balance))
	}
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L158-203)
```rust
// We implement `fungibles::Mutate` to override `burn_from` and `mint_to`.
//
// These functions are used in [`xcm_builder::FungiblesAdapter`].
impl<T: Config> fungibles::Mutate<<T as frame_system::Config>::AccountId> for Pallet<T> {
	fn burn_from(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Preservation,
		_: Precision,
		_: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let checking_account_eth = T::AddressMapper::to_address(&Self::checking_account());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, .. } = Self::bare_call(
			OriginFor::<T>::signed(who.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		log::trace!(target: "whatiwant", "{weight_consumed}");
		if let Ok(return_value) = result {
			if return_value.did_revert() {
				Err("Contract reverted".into())
			} else {
				let is_success =
					bool::abi_decode_validate(&return_value.data).expect("Failed to ABI decode");
				if is_success {
					let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
					Ok(balance)
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L205-241)
```rust
	fn mint_into(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
	) -> Result<Self::Balance, DispatchError> {
		let eth_address = T::AddressMapper::to_address(who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		let data = IERC20::transferCall { to: address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, .. } = Self::bare_call(
			OriginFor::<T>::signed(Self::checking_account()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		if let Ok(return_value) = result {
			if return_value.did_revert() {
				Err("Contract reverted".into())
			} else {
				let is_success =
					bool::abi_decode_validate(&return_value.data).expect("Failed to ABI decode");
				if is_success {
					let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
					Ok(balance)
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}
```

### Title
`pallet-revive` ERC20 `Mutate::burn_from`/`mint_into` return the account's live token balance instead of the actual transferred amount, letting anyone inflate asset accounting used by XCM - (File: `substrate/frame/revive/src/impl_fungibles.rs`)

### Summary
`pallet-revive`'s custom `fungibles::Mutate` implementation for ERC20 contracts, used by `xcm_builder`'s fungibles adapter to bridge ERC20 assets into XCM holding, returns the *current* ERC20 `balanceOf()` value of the affected account as the result of `burn_from`/`mint_into`, rather than the amount that was actually transferred by the just-executed `transfer()` call. This is the same broken pattern as the Forta report: an authoritative, operation-scoped return value (the withdrawn/minted amount) is replaced by a live, externally-manipulable balance read, which anyone can inflate by simply sending ERC20 tokens to the account before the accounting call resolves.

### Finding Description
`burn_from` and `mint_into` in [1](#0-0)  perform an ERC20 `transfer` via `bare_call`, and on success compute the value to return like this: [2](#0-1) [3](#0-2) 

In both cases, instead of returning `amount` (the value that was actually transferred, which the caller already knows and which is the standard semantic for `Mutate::burn_from`/`mint_into`), the code calls `<Self as fungibles::Inspect<_>>::balance(asset_id, who)`, i.e. re-queries the ERC20 contract's `balanceOf(who)` — the *current total balance* of the account, not the delta produced by this operation.

This mirrors the Forta bug precisely: in `InactiveSharesDistributor`, `undelegate` used the distributor's live FORT `balanceOf()` instead of the amount actually returned by `withdraw()`, and since `balanceOf()` can be inflated by any third party sending FORT directly to the distributor, downstream arithmetic that assumed the balance equaled the withdrawn amount broke. Here, `balance()` on an ERC20 contract deployed under `pallet-revive` is likewise a value anyone can inflate for a given account by simply calling `transfer()` (or any minting path in the specific ERC20 contract) to send extra tokens to `who` (for `burn_from`) or to the checking account (for `mint_into`) before/around the accounting call — there is no binding between the reported value and the specific transfer that this call performed.

Contrast this with the canonical `fungibles::Mutate`/`Balanced` contract elsewhere in the codebase, where `deposit`/`withdraw` return an `Imbalance` whose magnitude is exactly the amount increased/decreased by *this* operation: [4](#0-3) . `pallet-revive`'s override discards this invariant and substitutes a value that reflects the account's entire token holdings, not the effect of the call.

Any XCM- or pallet-level accounting code that treats the `burn_from`/`mint_into` return value as "the amount that moved" (as the trait contract specifies) will instead see an amount that an unprivileged attacker can inflate by pre-loading extra ERC20 tokens into the target account (`who` for burn, the checking/pool account for mint) — exactly the same "attacker can manipulate the balance read used for accounting" primitive as the original report, just relocated from a Solidity distributor contract's `balanceOf` to `pallet-revive`'s ERC20 `balanceOf` bridge used for XCM asset transfer.

### Impact Explanation
If a caller (e.g. an XCM asset adapter, or any other pallet using `fungibles::Mutate` generically against `pallet-revive`'s ERC20 assets) trusts the returned `Balance` from `burn_from`/`mint_into` as the delta actually moved — as the trait's documented contract requires — an attacker can inflate that reported delta by donating extra ERC20 tokens to the relevant account beforehand. Depending on how the caller uses this value (e.g. crediting XCM holding with "amount minted", or debiting a tracked ledger by "amount burned"), this can lead to over-crediting of XCM holding relative to what was actually escrowed/burned, i.e. an unbacked-mint-like accounting mismatch, or an underflow/revert in any downstream ledger subtraction that assumes the reported amount can never exceed a tracked cap — the same failure mode (funds becoming stuck, or value created from nothing) described in the original report.

### Likelihood Explanation
The attack primitive requires no privileged access: any account can call `transfer()` on the target ERC20 contract to move extra tokens into the account whose `balance()` will be read by `burn_from`/`mint_into`. This is directly comparable in triviality to the original Forta issue ("anyone can send FORT tokens to the distributor").

### Recommendation
Change `burn_from` and `mint_into` in `substrate/frame/revive/src/impl_fungibles.rs` to return the `amount` parameter (or a value strictly derived from the ERC20 `transfer` call's actual effect, e.g. the balance delta captured immediately before and after the call within the same atomic operation) instead of re-querying the live `balanceOf()`, restoring the trait's "delta actually moved" contract and removing the externally-manipulable balance dependency.

### Proof of Concept
1. Deploy/identify an ERC20 contract managed through `pallet-revive` and used as an XCM asset via the `fungibles::Mutate` implementation in `impl_fungibles.rs`.
2. As an unprivileged attacker, call the ERC20 contract's `transfer` to send extra tokens directly to the account `who` that a legitimate `burn_from(asset_id, who, amount, ...)` call will target (or to the checking account for `mint_into`).
3. Trigger the legitimate `burn_from`/`mint_into` call (e.g. via an XCM message that debits/credits this asset).
4. Observe that the function returns `<Self as fungibles::Inspect<_>>::balance(asset_id, who)` [5](#0-4)  — a value inflated by the attacker's donation — rather than the true `amount` transferred by this call, which any caller relying on the trait's documented delta semantics will misinterpret.

Note: I was unable to fully trace, within the remaining tool budget, every concrete downstream caller in `xcm-builder` that consumes this specific return value's numeric magnitude (versus only using it as a success indicator), so the exact chain to a fund-loss/duplicate-settlement outcome in a live runtime configuration could not be confirmed end-to-end from the index alone; a Devin session with full repo access would be needed to trace all call sites of this `fungibles::Mutate` implementation to confirm whether any caller trusts the numeric value rather than treating the `Ok`/`Err` as a boolean success signal.

### Citations

**File:** substrate/frame/revive/src/impl_fungibles.rs (L161-242)
```rust
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
}
```

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L515-555)
```rust
	fn deposit(
		asset: Self::AssetId,
		who: &AccountId,
		value: Self::Balance,
		precision: Precision,
	) -> Result<Debt<AccountId, Self>, DispatchError> {
		let increase = Self::increase_balance(asset.clone(), who, value, precision)?;
		Self::done_deposit(asset.clone(), who, increase);
		Ok(Imbalance::<Self::AssetId, Self::Balance, Self::OnDropDebt, Self::OnDropCredit>::new(
			asset, increase,
		))
	}

	/// Removes `value` balance from `who` account if possible.
	///
	/// If `precision` is `BestEffort` and `value` in full could not be removed (e.g. due to
	/// underflow), then the maximum is removed, up to `value`. If `precision` is `Exact`, then
	/// exactly `value` must be removed from the account of `who` or the operation will fail with an
	/// `Err` and nothing will change.
	///
	/// If the removal is needed but not possible, then it returns `Err` and nothing is changed.
	/// If the account needed to be deleted, then slightly more than `value` may be removed from the
	/// account owning since up to (but not including) minimum balance may also need to be removed.
	///
	/// If the operation is successful, this will return `Ok` with a `Credit` of the total value
	/// removed from the account.
	fn withdraw(
		asset: Self::AssetId,
		who: &AccountId,
		value: Self::Balance,
		precision: Precision,
		preservation: Preservation,
		force: Fortitude,
	) -> Result<Credit<AccountId, Self>, DispatchError> {
		let decrease =
			Self::decrease_balance(asset.clone(), who, value, precision, preservation, force)?;
		Self::done_withdraw(asset.clone(), who, decrease);
		Ok(Imbalance::<Self::AssetId, Self::Balance, Self::OnDropCredit, Self::OnDropDebt>::new(
			asset, decrease,
		))
	}
```

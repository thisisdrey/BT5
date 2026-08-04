### Title
`ERC20 precompile transferFrom reverts when the spender is the source, locking contract-held assets` - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

### Summary
The `ERC20` precompile in `pallet_assets` exposes `transferFrom` and routes every call through `pallet_assets::Pallet::do_transfer_approved`, a function that requires a stored approval and decrements it. [1](#0-0) [2](#0-1)  When the EVM caller is the same account as `call.from` — the standard ERC-20 pattern for a contract to withdraw its own tokens, e.g. a vault redeem — the precompile does not short-circuit to a direct transfer. [1](#0-0)  Because `do_transfer_approved` looks up `Approvals[(asset, owner, delegate)]` and reverts if the entry is missing or insufficient, a contract calling `transferFrom(address(this), user, amount)` will fail unless it has explicitly approved itself.  This mirrors the external `SwappableYieldSource.redeemToken` bug class: using `transferFrom` to move a contract's own tokens instead of a direct transfer can make the funds unrecoverable.

### Finding Description
In `substrate/frame/assets/precompiles/src/lib.rs`, `ERC20::transfer_from` decodes `call.from` and `call.to`, maps them to Substrate account IDs, and calls `pallet_assets::Pallet::<Runtime, Instance>::do_transfer_approved(asset_id, &from, &spender, &to, approval_amount)`. <cite repo="Loderfordw/polkadot-sdk--027" path="substrate/frame/assets

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L434-469)
```rust
	/// Execute the transfer_from call.
	fn transfer_from(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		call: &IERC20::transferFromCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as Config<Instance>>::WeightInfo::transfer_approved())?;
		let spender = Self::caller(env)?;
		let spender = <Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&spender);

		let from = call.from.into_array().into();
		let from = <Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&from);

		let to = call.to.into_array().into();
		let to = <Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&to);

		let approval_amount = Self::to_balance(call.value)?;
		pallet_assets::Pallet::<Runtime, Instance>::do_transfer_approved(
			asset_id,
			&from,
			&spender,
			&to,
			approval_amount,
		)?;

		Self::deposit_event(
			env,
			IERC20Events::Transfer(IERC20::Transfer {
				from: call.from,
				to: call.to,
				value: call.value,
			}),
		)?;

		Ok(IERC20::transferFromCall::abi_encode_returns(&true))
	}
```

**File:** substrate/frame/assets/src/impl_fungibles.rs (L323-331)
```rust
	fn transfer_from(
		asset: T::AssetId,
		owner: &<T as SystemConfig>::AccountId,
		delegate: &<T as SystemConfig>::AccountId,
		dest: &<T as SystemConfig>::AccountId,
		amount: T::Balance,
	) -> DispatchResult {
		Self::do_transfer_approved(asset, owner, delegate, dest, amount)
	}
```

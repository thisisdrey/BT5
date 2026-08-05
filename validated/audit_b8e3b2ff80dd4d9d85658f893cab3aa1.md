Audit Report

## Title
ERC20 asset-precompile state-changing calls do not reject non-zero native `value`, permanently locking mistakenly-sent native funds - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

## Summary
The `pallet-assets` ERC20 precompile's `ERC20::call` dispatcher never checks that the native value attached to a `transfer`, `approve`, `transferFrom`, or `permit` call is zero before executing the corresponding `pallet_assets` logic. Because `pallet_revive::Stack::run` unconditionally transfers any attached native value into the precompile's synthetic account before precompile logic runs, and this precompile has `HAS_CONTRACT_INFO = false` (no code path to move funds back out), any native value attached to these calls becomes permanently stranded.

## Finding Description
`ERC20::call` in `substrate/frame/assets/precompiles/src/lib.rs` only checks `env.is_delegate_call()` and `env.is_read_only()` before dispatching to `Self::transfer`, `Self::approve`, `Self::transfer_from`, and `Self::permit` — it never inspects the native value transferred with the call: [1](#0-0) 

Meanwhile, in `substrate/frame/revive/src/exec.rs`, `Stack::run`'s `do_transaction` closure unconditionally calls `Self::transfer_from_origin` to move `frame.value_transferred` into the callee's `account_id` *before* the precompile's `call`/`call_with_info` logic executes, for any non-delegate frame: [2](#0-1) 

`transfer_from_origin` delegates to `Stack::transfer`, which — if the destination account does not yet exist — atomically funds it with the existential deposit from the origin and then moves the value in, bringing the synthetic precompile account into existence with both ED and the attacker-supplied value: [3](#0-2) [4](#0-3) 

The only mechanism that would give a precompile a redeemable account (a consumer reference / contract info) is gated on `precompile.has_contract_info()`: [5](#0-4) 

For the ERC20 asset precompile, `HAS_CONTRACT_INFO` is explicitly `false`: [6](#0-5) 

This means the value transfer into the precompile's synthetic `AccountId` happens unconditionally regardless of contract info, but there is no logic anywhere in `ERC20<Runtime, PrecompileConfig, Instance>` that can move that native balance back out — no payable/withdraw entry point exists in `IERC20Calls`. Funds sent this way are permanently unreachable through the public interface.

## Impact Explanation
Any unprivileged caller who attaches non-zero native `value` to a `transfer`, `transferFrom`, `approve`, or `permit` call against an ERC20 asset-precompile address permanently loses that native balance, since it lands in a synthetic account with no reachable spend path. This matches the "permanent user-fund lock" impact category in the Polkadot SDK impact gate.

## Likelihood Explanation
This requires only a normal, unprivileged EOA or dApp/wallet caller to attach non-zero native value to what should be a value-free ERC20 call (e.g., due to wallet/SDK tooling defaults, mirroring the referenced Li.Fi incident). No privileged actor, malicious peer, or compromised infrastructure is needed — the bug is reachable purely through `pallet_revive::Pallet::call`/`bare_call` targeting a precompile address with `value > 0`.

## Recommendation
Add an explicit guard in `ERC20::call` (or in each of `transfer`, `approve`, `transfer_from`, `permit`) in `substrate/frame/assets/precompiles/src/lib.rs` that reverts (e.g., with `Error::Revert`) whenever the call's native value is non-zero. Apply the same guard to other precompiles with `HAS_CONTRACT_INFO = false` that lack a reclaim path for native value credited to their synthetic accounts (e.g., `pallet-asset-conversion` and `pallet-xcm` precompiles).

## Proof of Concept
1. Enable the `pallet-assets` ERC20 precompile for an asset id, yielding synthetic address `P`.
2. From a funded EOA, call `pallet_revive::Pallet::<Runtime>::bare_call` targeting `P` with `input = IERC20::transferCall{to, value}.abi_encode()` and a non-zero native `value` parameter on the call itself.
3. `Stack::run`'s `transfer_from_origin` moves the native value (plus ED if the account doesn't yet exist) into `P`'s underlying `AccountId` before `ERC20::transfer` executes; the ERC20 token transfer succeeds and the call returns `true` without reverting.
4. The native balance now held at `P`'s `AccountId` is unreachable — `ERC20::call` has no payable/withdraw branch, and `HAS_CONTRACT_INFO = false` means no contract logic will ever exist there to move it, permanently stranding the funds.

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L158-162)
```rust
	type T = Runtime;
	type Interface = IERC20::IERC20Calls;
	const MATCHER: AddressMatcher = PrecompileConfig::MATCHER;
	const HAS_CONTRACT_INFO: bool = false;

```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L163-192)
```rust
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

		let asset_id = PrecompileConfig::AssetIdExtractor::asset_id_from_address(address)?.into();
		let contract_addr = H160::from(*address);

		match input {
			// State-changing calls - check read-only
			IERC20Calls::transfer(_) |
			IERC20Calls::approve(_) |
			IERC20Calls::transferFrom(_) |
			IERC20Calls::permit(_)
				if env.is_read_only() =>
			{
				Err(Error::Error(pallet_revive::Error::<Self::T>::StateChangeDenied.into()))
			},

			// ERC20 functions
			IERC20Calls::transfer(call) => Self::transfer(asset_id, call, env),
			IERC20Calls::totalSupply(_) => Self::total_supply(asset_id, env),
			IERC20Calls::balanceOf(call) => Self::balance_of(asset_id, call, env),
			IERC20Calls::allowance(call) => Self::allowance(asset_id, call, env),
			IERC20Calls::approve(call) => Self::approve(asset_id, call, env),
```

**File:** substrate/frame/revive/src/exec.rs (L1375-1387)
```rust
			// Every non delegate call or instantiate also optionally transfers the balance.
			// If it is a delegate call, then we've already transferred tokens in the
			// last non-delegate frame.
			if frame.delegate.is_none() {
				Self::transfer_from_origin(
					&self.origin,
					&caller,
					account_id,
					frame.value_transferred,
					&mut frame.frame_meter,
					self.exec_config,
				)?;
			}
```

**File:** substrate/frame/revive/src/exec.rs (L1389-1405)
```rust
			// We need to make sure that the pre-compiles contract exist before executing it.
			// A few more conditionals:
			// 	- Only contracts with extended API (has_contract_info) are guaranteed to have an
			//    account.
			//  - Only when not delegate calling we are executing in the context of the pre-compile.
			//    Pre-compiles itself cannot delegate call.
			if let Some(precompile) = executable.as_precompile() &&
				precompile.has_contract_info() &&
				frame.delegate.is_none() &&
				!<System<T>>::account_exists(account_id)
			{
				// prefix matching pre-compiles cannot have a contract info
				// hence we only mint once per pre-compile
				T::Currency::mint_into(account_id, T::Currency::minimum_balance())?;
				// make sure the pre-compile does not destroy its account by accident
				<System<T>>::inc_consumers(account_id)?;
			}
```

**File:** substrate/frame/revive/src/exec.rs (L1738-1769)
```rust
		if <System<T>>::account_exists(to) {
			return transfer_with_dust::<T>(from, to, value, preservation);
		}

		let origin = origin.account_id()?;
		let ed = <T as Config>::Currency::minimum_balance();
		let is_eth_tx = exec_config.collect_deposit_from_hold.is_some();
		with_transaction(|| -> TransactionOutcome<DispatchResult> {
			// Meter the ED deposit only after the transfer succeeds: the meter is not rolled
			// back, so metering earlier would count an ED for an account never created.
			match Ok::<(), DispatchError>(())
				.and_then(|_| {
					if is_eth_tx {
						let credit = T::FeeInfo::withdraw_txfee(ed)
							.ok_or(Error::<T>::StorageDepositNotEnoughFunds)?;
						T::Currency::resolve(to, credit)
							.map_err(|_| Error::<T>::StorageDepositNotEnoughFunds)?;
						Ok(())
					} else {
						T::Currency::transfer(origin, to, ed, Preservation::Preserve)
							.map(|_| ())
							.map_err(|_| Error::<T>::StorageDepositNotEnoughFunds.into())
					}
				})
				.and_then(|_| transfer_with_dust::<T>(from, to, value, preservation))
				.and_then(|_| meter.charge_deposit(&StorageDeposit::Charge(ed)))
			{
				Ok(_) => TransactionOutcome::Commit(Ok(())),
				Err(err) => TransactionOutcome::Rollback(Err(err)),
			}
		})
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1771-1790)
```rust
	/// Same as `transfer` but `from` is an `Origin`.
	fn transfer_from_origin<S: State>(
		origin: &Origin<T>,
		from: &Origin<T>,
		to: &T::AccountId,
		value: U256,
		meter: &mut ResourceMeter<T, S>,
		exec_config: &ExecConfig<T>,
	) -> ExecResult {
		// If the from address is root there is no account to transfer from, and therefore we can't
		// take any `value` other than 0.
		let from = match from {
			Origin::Signed(caller) => caller,
			Origin::Root if value.is_zero() => return Ok(Default::default()),
			Origin::Root => return Err(DispatchError::RootNotAllowed.into()),
		};
		Self::transfer(origin, from, to, value, Preservation::Preserve, meter, exec_config)
			.map(|_| Default::default())
			.map_err(Into::into)
	}
```

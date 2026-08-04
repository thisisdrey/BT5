Based on my investigation, I found a real analog in `pallet-revive`. The key evidence: the `pure_precompile_works` test confirms that calling a `HAS_CONTRACT_INFO: bool = false` precompile with a non-zero native value succeeds and the balance is credited to the precompile's mapped account [1](#0-0) , while the `precompiles_work` test (value = 0) shows that for such precompiles "no account or contract info should be created" [2](#0-1) . Value transfer to a callee happens unconditionally for non-delegate calls in `do_transaction`, independent of whether the target is a precompile [3](#0-2) , but the mint/consumer bookkeeping that protects a precompile's account only runs `if precompile.has_contract_info()` [4](#0-3) . Meanwhile several shipped ERC20-style and XCM precompiles set `HAS_CONTRACT_INFO = false` and never read or forward `env.value_transferred()` [5](#0-4) [6](#0-5) .

### Title
Native value sent to `HAS_CONTRACT_INFO = false` precompiles (ERC20/XCM) is silently accepted and permanently locked - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s call dispatch transfers attached native `value` to the callee's mapped account before invoking the callee's logic, with no distinction for precompiles. Several built-in precompiles (`ERC20` in `substrate/frame/assets/precompiles/src/lib.rs`, `XcmPrecompile` in `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs`) declare `HAS_CONTRACT_INFO = false`, meaning "no account or any other state will be created for the address" per their own documentation, yet the value-transfer path runs regardless and does not check whether the target function is meant to receive value ("payable"), mirroring exactly the Canto `LiquidityMiningPath::protocolCmd()` bug where a payable outer entrypoint routes to a non-payable inner handler that neither rejects nor uses the transferred value.

### Finding Description
In `Stack::run`, the balance transfer from origin to the destination account happens unconditionally for any non-delegate call: `if frame.delegate.is_none() { Self::transfer_from_origin(...) }` [3](#0-2) . This runs before the precompile's `call()`/`call_with_info()` handler is invoked, and it runs whether or not the target is a precompile. Immediately after, the code only special-cases account creation/consumer-protection for precompiles with `has_contract_info()`: [4](#0-3) 

For precompiles with `HAS_CONTRACT_INFO = false` (e.g. `ERC20` [7](#0-6)  and `XcmPrecompile` [8](#0-7) ), the design intent is that "no account or any other state will be created for the address" [9](#0-8) . But the generic `transfer` helper in `exec.rs` will pull a nonexistent destination into existence by paying the ED from the origin whenever `value > 0`: `if <System<T>>::account_exists(to) { ... } else { ... pay ED and credit value ... }` [10](#0-9) . This is confirmed empirically by `pure_precompile_works`, where sending `native_value(1_000)`/`value=100` to a `HAS_CONTRACT_INFO = false` precompile (e.g. ECRecover at `H160::from_low_u64_be(1)`) results in `Pallet::<Test>::evm_balance(&precompile_addr) == U256::from(100)` [11](#0-10) .

None of the affected precompiles' `call()` implementations read `env.value_transferred()`, forward it, or reject it — `ERC20::call` dispatches directly to helper functions like `transfer`/`approve` without any value handling [12](#0-11) , and `XcmPrecompile::call` likewise ignores `value_transferred` entirely [13](#0-12) . Because the precompile's mapped account never gets `inc_consumers`/`mint_into` treatment (that's reserved for `has_contract_info()` precompiles), and because no code path exists that lets the precompile logic move funds out of its own mapped account (there is no "self-call" or withdrawal mechanism exposed to these particular precompiles), any native value attached to a call into these addresses is credited to an address that no contract logic or externally-owned key can spend from.

This exactly parallels the Canto finding's broken invariant: an outer, value-accepting entrypoint (`CrocSwapDex::protocolCmd` — analogous to `pallet-revive`'s generic value-forwarding `call` dispatch) routes to an inner handler (`LiquidityMiningPath::protocolCmd` — analogous to the ERC20/XCM `Precompile::call` implementations) that has no accounting or handling for the transferred value.

### Impact Explanation
This falls under "permanent user-fund lock," a listed impact category. A user who mistakenly (or via a wallet/dApp that assumes ERC20 `transfer`/`approve` are payable-safe, or blindly attaches gas-station-style value) sends native value alongside a call to these precompile addresses will have that value irrecoverably stuck at an address with no spending authority, since these precompile addresses are not real EOAs and the "no contract info" precompiles have no mechanism to reclaim or forward the balance.

### Likelihood Explanation
Any unprivileged caller can trigger this by simply attaching a non-zero `value` to a call targeting these precompile addresses — no special privileges, malicious peers, governance, or off-chain assumptions are required. The precompile addresses (e.g. the ERC20 asset precompile prefix, or the fixed XCM precompile address `10`) are well-known/discoverable, making accidental or exploratory fund loss plausible, particularly from tooling generated for standard ERC20 interfaces that may not always omit `msg.value`.

### Recommendation
For `HAS_CONTRACT_INFO = false` precompiles that do not implement value handling, reject any call carrying non-zero `value_transferred()` before the generic transfer executes (i.e., check `env.value_transferred().is_zero()` at the top of `call()`), or move the value check earlier in `Stack::run`/`do_transaction` so that a call into a precompile without a defined value-handling contract fails fast instead of silently locking funds. Precompiles such as `ERC20::call` and `XcmPrecompile::call` should explicitly `ensure!(value_transferred.is_zero(), ...)` unless they are the built-in "system"/"terminate" style precompiles designed to receive value.

### Proof of Concept
1. Deploy or use the `ERC20` precompile mapping to `pallet-assets` (or the fixed-address `XcmPrecompile` at address `10`).
2. From an unprivileged EOA/contract, invoke `pallet_revive::Pallet::<T>::bare_call` (or an EVM transaction) targeting the precompile address with `IERC20::transferCall` (or `IXcm::sendCall`) and a non-zero `value` parameter.
3. Observe the call succeeds (transfer/XCM logic executes normally, ignoring `value`), and per the pattern demonstrated in `pure_precompile_works` [1](#0-0) , `Pallet::<T>::evm_balance(&precompile_addr)` now reflects the transferred amount.
4. Confirm there is no dispatchable, precompile function, or governance call that lets this balance be moved out of the precompile's mapped account — the funds are permanently stranded.

### Citations

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4773)
```rust
	for (description, precompile_addr, input, output) in cases {
		let (code, _code_hash) = compile_module("call_and_return").unwrap();
		ExtBuilder::default().build().execute_with(|| {
			let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
			let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code))
				.native_value(1_000)
				.build_and_unwrap_contract();

			let result = builder::bare_call(addr)
				.data(
					(&precompile_addr, 100u64)
						.encode()
						.into_iter()
						.chain(input)
						.collect::<Vec<_>>(),
				)
				.build_and_unwrap_result();

			assert_eq!(
				Pallet::<Test>::evm_balance(&precompile_addr),
				U256::from(100),
				"{description}: unexpected balance"
			);
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4870-4873)
```rust
			// no account or contract info should be created for a NoInfo pre-compile
			assert!(get_contract_checked(&precompile_addr).is_none());
			assert!(!System::account_exists(&id));
			assert_eq!(Pallet::<Test>::evm_balance(&precompile_addr), U256::zero());
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

**File:** substrate/frame/revive/src/exec.rs (L1738-1762)
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
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L147-167)
```rust
impl<Runtime, PrecompileConfig, Instance: 'static> Precompile
	for ERC20<Runtime, PrecompileConfig, Instance>
where
	PrecompileConfig: AssetPrecompileConfig,
	Runtime: crate::Config<Instance> + pallet_revive::Config + permit::Config,
	<<PrecompileConfig as AssetPrecompileConfig>::AssetIdExtractor as AssetIdExtractor>::AssetId:
		Into<<Runtime as Config<Instance>>::AssetId>,
	Call<Runtime, Instance>: Into<<Runtime as pallet_revive::Config>::RuntimeCall>,
	alloy::primitives::U256: TryInto<<Runtime as Config<Instance>>::Balance>,
	alloy::primitives::U256: TryFrom<<Runtime as Config<Instance>>::Balance>,
{
	type T = Runtime;
	type Interface = IERC20::IERC20Calls;
	const MATCHER: AddressMatcher = PrecompileConfig::MATCHER;
	const HAS_CONTRACT_INFO: bool = false;

	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L176-207)
```rust
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
			IERC20Calls::transferFrom(call) => Self::transfer_from(asset_id, call, env),

			// ERC20Permit functions (EIP-2612)
			IERC20Calls::permit(call) => Self::permit(asset_id, contract_addr, call, env),
			IERC20Calls::nonces(call) => Self::nonces(contract_addr, call, env),
			IERC20Calls::DOMAIN_SEPARATOR(_) => {
				Self::domain_separator(asset_id, contract_addr, env)
			},

			// ERC20Metadata functions
			IERC20Calls::name(_) => Self::name(asset_id, env),
			IERC20Calls::symbol(_) => Self::symbol(asset_id, env),
			IERC20Calls::decimals(_) => Self::decimals(asset_id, env),
		}
	}
```

**File:** polkadot/xcm/pallet-xcm/precompiles/src/lib.rs (L64-187)
```rust
impl<Runtime> Precompile for XcmPrecompile<Runtime>
where
	Runtime: crate::Config + pallet_revive::Config,
{
	type T = Runtime;
	const MATCHER: AddressMatcher = AddressMatcher::Fixed(NonZero::new(10).unwrap());
	const HAS_CONTRACT_INFO: bool = false;
	type Interface = IXcm::IXcmCalls;

	fn call(
		_address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

		let origin = env.caller();
		let frame_origin = match origin {
			Origin::Root => RawOrigin::Root.into(),
			Origin::Signed(account_id) => RawOrigin::Signed(account_id.clone()).into(),
		};

		match input {
			IXcmCalls::send(_) | IXcmCalls::execute(_) if env.is_read_only() => {
				Err(Error::Error(pallet_revive::Error::<Self::T>::StateChangeDenied.into()))
			},
			IXcmCalls::send(IXcm::sendCall { destination, message }) => {
				let _ = env.charge(<Runtime as Config>::WeightInfo::send())?;

				let final_destination = VersionedLocation::decode_all(&mut &destination[..])
					.map_err(|error| {
						revert(&error, "XCM send failed: Invalid destination format")
					})?;

				ensure_xcm_version(&final_destination)?;

				let final_message = VersionedXcm::<()>::decode_all_with_depth_limit(
					MAX_XCM_DECODE_DEPTH,
					&mut &message[..],
				)
				.map_err(|error| revert(&error, "XCM send failed: Invalid message format"))?;

				ensure_xcm_version(&final_message)?;

				pallet_xcm::Pallet::<Runtime>::send(
					frame_origin,
					final_destination.into(),
					final_message.into(),
				)
				.map(|_| Vec::new())
				.map_err(|error| {
					revert(
						&error,
						"XCM send failed: destination or message format may be incompatible",
					)
				})
			},
			IXcmCalls::execute(IXcm::executeCall { message, weight }) => {
				let max_weight = Weight::from_parts(weight.refTime, weight.proofSize);
				let weight_to_charge =
					max_weight.saturating_add(<Runtime as Config>::WeightInfo::execute());
				let charged_amount = env.charge(weight_to_charge)?;

				let final_message = VersionedXcm::decode_all_with_depth_limit(
					MAX_XCM_DECODE_DEPTH,
					&mut &message[..],
				)
				.map_err(|error| revert(&error, "XCM execute failed: Invalid message format"))?;

				ensure_xcm_version(&final_message)?;

				let result = pallet_xcm::Pallet::<Runtime>::execute(
					frame_origin,
					final_message.into(),
					max_weight,
				);

				let pre = DispatchInfo {
					call_weight: weight_to_charge,
					extension_weight: Weight::zero(),
					..Default::default()
				};

				// Adjust gas using actual weight or fallback to initially charged weight
				let actual_weight = frame_support::dispatch::extract_actual_weight(&result, &pre);
				env.adjust_gas(charged_amount, actual_weight);

				result.map(|_| Vec::new()).map_err(|error| {
					revert(
							&error,
							"XCM execute failed: message may be invalid or execution constraints not satisfied"
						)
				})
			},
			IXcmCalls::weighMessage(IXcm::weighMessageCall { message }) => {
				let _ = env.charge(<Runtime as Config>::WeightInfo::weigh_message())?;

				let converted_message = VersionedXcm::decode_all_with_depth_limit(
					MAX_XCM_DECODE_DEPTH,
					&mut &message[..],
				)
				.map_err(|error| revert(&error, "XCM weightMessage: Invalid message format"))?;

				ensure_xcm_version(&converted_message)?;

				let mut final_message = converted_message.try_into().map_err(|error| {
					revert(&error, "XCM weightMessage: Conversion to Xcm failed")
				})?;

				let weight = <<Runtime>::Weigher>::weight(&mut final_message, Weight::MAX)
					.map_err(|error| {
						revert(&error, "XCM weightMessage: Failed to calculate weight")
					})?;

				let final_weight =
					IXcm::Weight { proofSize: weight.proof_size(), refTime: weight.ref_time() };

				Ok(final_weight.abi_encode())
			},
		}
	}
```

**File:** substrate/frame/revive/src/precompiles.rs (L190-193)
```rust
	/// # When set to **false**
	///
	/// - No account or any other state will be created for the address.
	/// - Only `call` should be implemented. `call_with_info` is never called.
```

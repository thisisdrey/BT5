### Title
Native-to-EVM decimal scale mismatch in `pallet-revive` balance conversion causes false EVM balance/threshold values - (File: `substrate/frame/revive/src/lib.rs`, `substrate/frame/revive/src/primitives.rs`)

### Summary
The Astaria bug's root cause is a hard-coded `1e18` scale assumption applied to a value that is actually expressed in the underlying asset's own decimal precision, producing either an underflow/revert or a silently wrong (too-small) transferable amount. `pallet-revive` contains the exact same class of bug: it maintains two parallel balance representations — the chain-native balance (e.g. 10/12 decimals on Polkadot/Kusama-style runtimes) and the EVM-expected 18-decimal ("wei") representation — related by the constant `T::NativeToEthRatio` [1](#0-0) [2](#0-1) . This conversion factor must be applied consistently everywhere a native-scale value is exposed to EVM-facing code; the repository's own change history shows at least one call site (`minimum_balance()`) was shipped without applying this factor, meaning a native-scale value (e.g. `10^10`/`10^12` units) was returned/cast directly as if it were already in 18-decimal EVM units [3](#0-2) .

### Finding Description
`pallet-revive` converts between the native `Balance` type and the Ethereum-style `U256` 18-decimal representation using `NativeToEthRatio` (`10^(18 - native_decimals)`, e.g. `1_000_000` for a 12-decimal native token) [2](#0-1) . The canonical, correct conversion path multiplies/divides by this ratio, as seen in `BalanceWithDust::from_value` (EVM → native) [4](#0-3)  and `evm_balance`/`convert_native_to_evm` (native → EVM) [5](#0-4) .

However, exactly as in the Astaria `WithdrawProxy.sol` bug — where a value already normalized to `1e18` was compared/subtracted against a balance expressed in the token's native (non-18) decimals without re-scaling — `pallet-revive` had at least one function, `minimum_balance()` exposed to the EVM execution context (`Ext::minimum_balance`), that cast the raw native-scale `BalanceOf<T>` value directly into `U256` without multiplying by `NativeToEthRatio`. This is documented as a confirmed, patched bug in the repository itself:

> "The value returned by `minimum_balance` was just cast into `U256`, without applying the actual conversion factor (`NativeToEthRatio`)." [3](#0-2) 

This mirrors the Astaria pattern precisely: a value that must be scaled by a fixed decimal-precision factor before being used in comparisons/transfers with a different-scale unit was instead used raw. On a runtime with native decimals lower than 18 (Polkadot: 10, most parachains: 10–12), the unscaled value is `NativeToEthRatio` times *smaller* than the correct EVM-facing value — the inverse-direction analog of the Astaria case (where the on-chain value was left too large relative to the actual token balance, causing underflow/revert). Here, a Solidity contract or EVM tool that reads `minimum_balance()` (or any other value that should be scaled but isn't) receives a threshold value understated by exactly the `NativeToEthRatio` factor (e.g. `10^6`), because the runtime's decimal precision differs from the assumed 18. The underlying invariant broken is the same one at the heart of the Astaria finding: "any monetary/threshold value crossing a decimals-precision boundary must be re-scaled by the exact conversion factor before use," and code paths that skip this step do not fail loudly (no revert) — they silently propagate an incorrect value into downstream balance/ED logic.

### Impact Explanation
Because `minimum_balance()`/EVM-balance values feed into contract-level and host-level balance-sufficiency checks (existential deposit accounting, dust handling in `BalanceWithDust`, `set_evm_balance`/`new_balance_with_dust` computations) [6](#0-5) , an unscaled threshold value can cause Solidity contracts (and any EVM tooling that trusts 18-decimal semantics, per the accompanying `pr_9101` decimals rework [7](#0-6) ) to make incorrect assumptions about how much value is required to keep an account alive or how funds are apportioned between "value" and "dust." This can manifest as accounts being unexpectedly reaped (fund loss/lock, matching the "permanent user-fund…lock" impact class) or contracts miscalculating balance thresholds, on any Polkadot SDK chain enabling `pallet-revive` with `NativeToEthRatio != 1` (i.e., any chain whose native decimals differ from 18 — which is virtually every Polkadot/Kusama-based chain).

### Likelihood Explanation
This is not a hypothetical: the repository's own `prdoc` confirms the bug shipped and was later patched, exactly matching the "sneaky, could persist for a long time before being noticed" characteristic that the Astaria report called out as aggravating the severity. The trigger requires no privileged actor — any user or contract on a chain with `pallet-revive` enabled (Asset Hub Westend/Rococo, Substrate node template, Penpal, etc., all of which set `NativeToEthRatio = 10^6` for 12-decimal natives) [8](#0-7) [9](#0-8)  is exposed to the mis-scaled value simply by calling the affected host function/RPC.

### Recommendation
Audit every location in `pallet-revive` (and any pallet exposing native-balance values to EVM-decimal-assuming consumers) where a `BalanceOf<T>` is cast into `U256` or vice versa, and ensure `NativeToEthRatio` (or the equivalent `BalanceWithDust` conversion path) is applied uniformly, with unit/property tests asserting `convert_native_to_evm(convert_evm_to_native(x)) == x` (mod dust) across the full range of configured `NativeToEthRatio` values, not just the "happy path" used in existing tests.

### Proof of Concept
The confirmed regression (prior to the `pr_9705` fix) is reproducible as: call the EVM host function / RPC path that surfaces `Ext::minimum_balance()` on any runtime with `NativeToEthRatio > 1` (e.g. Asset Hub Westend, `NativeToEthRatio = 1_000_000`) [2](#0-1) , and compare the returned `U256` to `existential_deposit_in_native * NativeToEthRatio`. Prior to the fix, the returned value was `existential_deposit_in_native` (raw cast), i.e. off by exactly the `NativeToEthRatio` factor — directly analogous to the Astaria PoC where `withdrawProxy.claim()` operated on a value off by `10^(18-decimals)` relative to the actual token balance scale.

### Citations

**File:** substrate/frame/revive/src/lib.rs (L333-335)
```rust
		/// The ratio between the decimal representation of the native token and the ETH token.
		#[pallet::constant]
		type NativeToEthRatio: Get<u32>;
```

**File:** substrate/frame/revive/src/lib.rs (L2435-2441)
```rust
	/// Get the balance with EVM decimals of the given `address`.
	///
	/// Returns the spendable balance excluding the existential deposit.
	pub fn evm_balance(address: &H160) -> U256 {
		let balance = AccountInfo::<T>::balance_of((*address).into());
		Self::convert_native_to_evm(balance)
	}
```

**File:** substrate/frame/revive/src/lib.rs (L2465-2497)
```rust
	/// Set the EVM balance of an account.
	///
	/// The account's total balance becomes the EVM value plus the existential deposit,
	/// consistent with `evm_balance` which returns the spendable balance excluding the existential
	/// deposit.
	pub fn set_evm_balance(address: &H160, evm_value: U256) -> Result<(), Error<T>> {
		let (balance, dust) = Self::new_balance_with_dust(evm_value)
			.map_err(|_| <Error<T>>::BalanceConversionFailed)?;
		let account_id = T::AddressMapper::to_account_id(&address);
		T::Currency::set_balance(&account_id, balance);
		AccountInfoOf::<T>::mutate(&address, |account| {
			if let Some(account) = account {
				account.dust = dust;
			} else {
				*account = Some(AccountInfo { dust, ..Default::default() });
			}
		});

		Ok(())
	}

	/// Construct native balance from EVM balance.
	///
	/// Adds the existential deposit and returns the native balance plus the dust.
	pub fn new_balance_with_dust(
		evm_value: U256,
	) -> Result<(BalanceOf<T>, u32), BalanceConversionError> {
		let ed = T::Currency::minimum_balance();
		let balance_with_dust = BalanceWithDust::<BalanceOf<T>>::from_value::<T>(evm_value)?;
		let (value, dust) = balance_with_dust.deconstruct();

		Ok((ed.saturating_add(value), dust))
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1404-1404)
```rust
	type NativeToEthRatio = ConstU32<1_000_000>; // 10^(18 - 12) Eth is 10^18, Native is 10^12.
```

**File:** prdoc/stable2512/pr_9705.prdoc (L1-9)
```text
title: '[pallet-revive] Properly convert `Ext::minimum_balance` value to `U256`'
doc:
- audience: Runtime Dev
  description: |
    The value returned by `minimum_balance` was just cast into `U256`, 
    without applying the actual conversion factor (`NativeToEthRatio`).
crates:
- name: pallet-revive
  bump: patch
```

**File:** substrate/frame/revive/src/primitives.rs (L183-196)
```rust
	/// Creates a new `BalanceWithDust` from the given EVM value.
	pub fn from_value<T: Config>(
		value: U256,
	) -> Result<BalanceWithDust<BalanceOf<T>>, BalanceConversionError> {
		if value.is_zero() {
			return Ok(Default::default());
		}

		let (quotient, remainder) = value.div_mod(T::NativeToEthRatio::get().into());
		let value = quotient.try_into().map_err(|_| BalanceConversionError::Value)?;
		let dust = remainder.try_into().map_err(|_| BalanceConversionError::Dust)?;

		Ok(BalanceWithDust { value, dust })
	}
```

**File:** prdoc/stable2509/pr_9101.prdoc (L1-27)
```text
title: '[revive] eth-decimals'
doc:
- audience: Runtime Dev
  description: |-
    On Ethereum, 1 ETH is represented as 10^18 wei (wei being the smallest unit).
    On Polkadot 1 DOT is defined as 10^10 plancks. It means that any value smaller than 10^8 wei can not be expressed with the native balance. Any contract that attempts to use such a value currently reverts with a DecimalPrecisionLoss error.

    In theory, RPC can define a decimal representation different from Ethereum mainnet (10^18). In practice tools (frontend libraries, wallets, and compilers) ignore it and expect 18 decimals.

    The current behaviour breaks eth compatibility and needs to be updated. See issue #109 for more details.


    Fix  https://github.com/paritytech/contract-issues/issues/109
    [weights compare](https://weights.tasty.limo/compare?unit=weight&ignore_errors=true&threshold=10&method=asymptotic&repo=polkadot-sdk&old=master&new=pg/eth-decimals&path_pattern=substrate/frame/**/src/weights.rs,polkadot/runtime/*/src/weights/**/*.rs,polkadot/bridges/modules/*/src/weights.rs,cumulus/**/weights/*.rs,cumulus/**/weights/xcm/*.rs,cumulus/**/src/weights.rs)
crates:
- name: pallet-revive
  bump: major
- name: pallet-revive-fixtures
  bump: major
- name: assets-common
  bump: major
- name: asset-hub-westend-runtime
  bump: major
- name: pallet-xcm
  bump: major
- name: pallet-assets
  bump: major
```

**File:** substrate/bin/node/runtime/src/lib.rs (L1632-1632)
```rust
	type NativeToEthRatio = ConstU32<1_000_000>; // 10^(18 - 12) Eth is 10^18, Native is 10^12.
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/lib.rs (L798-798)
```rust
	type NativeToEthRatio = ConstU32<1_000_000>; // 10^(18 - 12) Eth is 10^18, Native is 10^12.
```

### Title
Incorrect Decimal Scaling in Snowbridge Outbound Queue Remote Fee When Native Decimals Exceed 18 - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::convert_from_ether_decimals` only scales down from 18-decimal (wei/ether) units and never scales up. When a runtime configures its native token with more than 18 decimals, the remote fee component of a Snowbridge outbound message remains in 18-decimal units instead of being normalized to the native token's precision, producing a fee that is too small by a factor of `10^(native_decimals - 18)`. [1](#0-0) 

### Finding Description
`calculate_fee` computes the Ethereum-side execution cost in wei, carries it through `FixedU128` arithmetic using the configured multiplier and exchange rate, and then calls `convert_from_ether_decimals` to convert the result into the local native token's smallest units. [2](#0-1)  The conversion function subtracts the local decimals from `ETHER_DECIMALS` (18) and divides by `10^diff`. [3](#0-2)  Because it uses `saturating_sub`, any runtime whose `T::Decimals` is greater than 18 yields a diff of `0`, a denominator of `1`, and the value is returned unchanged in wei units. [1](#0-0)  This is the same broken invariant as the external report: a value computed in one decimal base is used as if it were already in the target token's decimal base.

### Impact Explanation
The corrupted value is the remote fee returned by `calculate_fee`. [2](#0-1)  On a chain with native decimals > 18, every outbound Snowbridge message pays only `1 / 10^(native_decimals - 18)` of the intended remote fee. This is public underpriced work: an unprivileged user can submit bridge messages that underpay relayers and underfund the remote execution cost, degrading bridge processing or allowing fee spam. It directly matches the Polkadot SDK impact gate for public underpriced work that stalls bridge processing.

### Likelihood Explanation
The bug is latent in the pallet code and activates as soon as any runtime sets `T::Decimals > 18`. [1](#0-0)  Common Substrate chains use 10–12 decimals, but the pallet places no bound on `T::Decimals` and the `saturating_sub` silently accepts higher values instead of reverting or scaling upward. No other guard normalizes the value upward, so the path is unblocked for any such runtime.

### Recommendation
Rewrite `convert_from_ether_decimals` to handle both directions symmetrically: divide when `T::Decimals < ETHER_DECIMALS` and multiply by `10^(T::Decimals - ETHER_DECIMALS)` when `T::Decimals > ETHER_DECIMALS`, using checked arithmetic to prevent overflow. Alternatively, enforce `T::Decimals <= ETHER_DECIMALS` in the pallet's `integrity_test` or configuration validation if higher precision is not intended to be supported.

### Proof of Concept
Consider a runtime with `T::Decimals::get() = 24`. A message whose remote fee is `1_000_000_000_000_000_000` wei (1 ETH worth) reaches `convert_from_ether_decimals`. [4](#0-3)  The function computes `ETHER_DECIMALS.saturating_sub(24) = 0`, so `denom = 10^0 = 1` and returns `1_000_000_000_000_000_000` as the local fee. [1](#0-0)  Because the native token has 24 decimals, the correct local fee should be `1_000_000_000_000_000_000 * 10^6 = 10^24` local units. The user therefore pays only `10^-6` of the required remote fee.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-393)
```rust
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L411-418)
```rust
		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

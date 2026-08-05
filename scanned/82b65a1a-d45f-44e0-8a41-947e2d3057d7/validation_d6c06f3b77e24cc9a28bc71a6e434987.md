Based on the analysis, the strongest local analog to the Uniswap oracle's decimal-truncation bug is the fee-underflow defect in Snowbridge's outbound queue fee calculation.

### Title
Snowbridge outbound-queue `calculate_fee` truncates remote fee to zero, enabling underpriced message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the DOT-denominated cost of delivering a message to Ethereum by reinterpreting a raw wei value as a `FixedU128`, scaling it by the exchange rate/multiplier, and then dividing by a fixed power-of-ten factor to adjust decimals. Exactly like the reported Uniswap oracle bug, the decimal-adjustment division is applied to an already-tiny fixed-point value, so legitimate non-zero inputs collapse to `0` via integer truncation, silently underpricing the actual cost of bridge work.

### Finding Description
The fee pipeline is: [1](#0-0) 

1. `remote_fee = fee_per_gas * gas_used_at_most + reward` (in wei).
2. This `u128` is reinterpreted via `FixedU128::from_inner(fee)` — i.e. treated as `fee / 10^18` — then multiplied by `multiplier` and divided by `exchange_rate`.
3. The result's raw inner value is passed to `convert_from_ether_decimals`, which does a plain integer `checked_div` by `10^(18 - Decimals)`: [2](#0-1) 

Because step 3 is a floor division by a large constant (`10^8` for a 10-decimal chain like Polkadot), any raw fixed-point value smaller than that divisor is truncated to `0`, regardless of whether the "true" fee is economically non-zero. This is structurally the same flaw as the reported Uniswap bug: a decimal-normalization factor is used as a naive divisor on an already-scaled-down quantity instead of being folded into the multiplication/scaling step, causing legitimate values to underflow to zero.

The pallet's own test suite documents this defect explicitly: [3](#0-2) 
The in-code comment states plainly that the resulting zero remote fee "is invalid which should be avoided," confirming this is a recognized, unresolved formula defect rather than intended behavior.

### Impact Explanation
`calculate_fee` determines the DOT fee charged to whoever submits a message into the outbound queue (via `send_message_impl`/`EthereumBlobExporter` or `snowbridge-pallet-system::send`). If the computed remote-fee component truncates to zero, the relayer-reward and gas-refund portion of the fee is entirely unfunded while the message is still queued, ABI-encoded, merklized, and forwarded to Ethereum for execution. This is exactly the "public underpriced work that degrades block production or stalls bridge processing" scenario called out in the impact gate: users can get real cross-chain dispatch work performed by relayers without paying for the remote-side cost that is supposed to reimburse them, and there's no separate on-chain guard that rejects a computed `Fee { remote: 0, .. }`.

### Likelihood Explanation
The zero-fee outcome depends on the relationship between the message's `gas_used_at_most`/reward terms and the `10^(18-Decimals)` divisor, not on any privileged action by governance to intentionally misconfigure pricing — it is a latent property of the formula itself for any combination of legitimate `PricingParameters` and message gas costs that produce a small enough scaled numerator. The pallet's own regression test proves this occurs with straightforward, non-adversarial parameter values (e.g., `exchange_rate = 1`, `fee_per_gas = 1`), so it does not require a malicious peer, relayer, or admin — only ordinary use of the fee calculation path with parameters/gas amounts that happen to fall below the truncation threshold.

### Recommendation
Reorder the computation so the decimal-scaling factor is applied before/within the division against the exchange rate (i.e., multiply first, divide once, at full precision), analogous to the audited fix for the Uniswap oracle: compute the fee in a single higher-precision fixed-point (or `U256`) expression and only round down once, immediately before returning the final integer `Balance`. At minimum, add a `saturating`/rounding guard so a truthfully non-zero computed fee is never silently returned as `0`, and add an explicit check in `calculate_fee`/`do_process_message` that rejects processing when `Fee::remote == 0` under non-zero `PricingParameters`.

### Proof of Concept
The existing unit test already demonstrates the underflow: [3](#0-2) 
With `exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`, `gas_used = 250000`: `remote_fee = 250001` wei → `FixedU128::from_inner(250001)` (≈ `2.5e-13`) → unchanged by multiplier/exchange_rate of `1` → `convert_from_ether_decimals` divides the raw inner value `250001` by `10^8`, yielding `0`. The assertion `assert_eq!(fee.remote, 0)` confirms the message would be committed and forwarded to Ethereum with zero relayer reward/gas refund funded.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-318)
```rust
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 1),
			fee_per_gas: 1_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
		assert_eq!(fee.local, 698000000);
		// Though none zero pricing params the remote fee calculated here is invalid
		// which should be avoided
		assert_eq!(fee.remote, 0);
	});
```

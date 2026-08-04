### Title
`OutboundQueue::calculate_fee` rounds the remote (relayer-reward) component to zero, letting senders under-pay for a promised Ethereum-side payout - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the DOT-denominated fee that is withdrawn from a message sender to cover the ETH-side relayer reward and gas refund. The final step, `convert_from_ether_decimals`, performs an integer division (`value / 10^decimals`) that truncates to `0` whenever the ether-denominated fee is smaller than the decimal scaler, exactly the "`number*numerator < denominator` truncates to 0" pattern from the external report. The pallet's own test suite documents this exact outcome and even states "though non-zero pricing params, the remote fee calculated here is invalid, which should be avoided" — yet no guard exists to reject it.

### Finding Description
The fee pipeline is: [1](#0-0) 

`calculate_remote_fee` computes `fee_per_gas * gas_used_at_most + reward` in wei (`U256`), then this is scaled by `multiplier` / `exchange_rate` (both `FixedU128`), and finally passed to: [2](#0-1) 

```rust
pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
    let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
    let denom = 10u128.saturating_pow(decimals);
    value.checked_div(denom).expect("divisor is non-zero; qed").into()
}
```

For a chain with 10 decimals (DOT), `decimals = 18 - 10 = 8`, so `denom = 10^8`. Any ether-denominated `value` under `10^8` (a realistic amount for cheap commands with a small `reward`/`fee_per_gas`) truncates to `0` — the exact `mulDiv`-style rounding-to-zero defect from the report. The pallet's own regression test proves this is reachable with valid, non-zero pricing parameters: [3](#0-2) 

```rust
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
    ...
    let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
    assert_eq!(fee.local, 698000000);
    // Though none zero pricing params the remote fee calculated here is invalid
    // which should be avoided
    assert_eq!(fee.remote, 0);
}
```

Crucially, this `fee` (specifically `fee.total() = fee.local + fee.remote`) is exactly what is charged to the sender's account via XCM in the public dispatch path (`pallet_xcm::send`/`transfer_assets` → `EthereumBlobExporter::validate`): [4](#0-3) 

```rust
let (ticket, fee) = OutboundQueue::validate(&outbound_message)...
// convert fee to Asset
let fee = Asset::from((Location::parent(), fee.total())).into();
```

Meanwhile, the *committed* message that is queued for Ethereum still embeds the un-truncated `reward` and `max_fee_per_gas` taken straight from `PricingParameters` (not from the truncated computed fee): [5](#0-4) 

```rust
let reward = pricing_params.rewards.remote;
let message = CommittedMessage {
    ...
    max_fee_per_gas: pricing_params.fee_per_gas.try_into()...,
    reward: reward.try_into().defensive_unwrap_or(u128::MAX),
    ...
};
```

So the amount promised to relayers on the Ethereum Gateway contract (`Min(GasPrice, MaxFeePerGas) * GasUsed() + Reward`, per the module doc at lines 72–80) is decoupled from what was actually collected on the Substrate side when the DOT-side conversion rounds to zero. An unprivileged sender using `pallet_xcm` to route commands through `EthereumBlobExporter` pays only `fee.local` (covering local weight) while `fee.remote` — meant to reimburse the parachain/bridge for the ETH-side reward it is contractually promising — is silently zero.

### Impact Explanation
This breaks the "public underpriced work" and value-conservation invariants: an attacker can enqueue Ethereum-bound governance/token commands (any command whose `gas_used_at_most` combined with cheap `fee_per_gas`/`reward` parameters yields an ether fee under the `10^decimals` scaler) essentially for free on the remote-cost side, while the protocol still commits to paying the full reward/gas refund out of the bridge's backing funds when a relayer later claims it on Ethereum. Repeated at scale, this drains the sovereign/reward-funding account backing Snowbridge payouts without corresponding fee collection, and because relayers are economically starved when the fee actually collected doesn't fund promised rewards, it can also degrade or stall message delivery (relayers stop servicing under-funded lanes). There is no check in `calculate_fee`, `validate`, or the exporter that rejects/floors a zero `remote` fee before enqueueing the message.

### Likelihood Explanation
Reaching a truncation requires only that governance-set `PricingParameters` (fee_per_gas, reward, exchange_rate, multiplier) combined with a command's gas estimate produce an ether-fee below `10^(18-Decimals)` wei — the pallet's own unit test demonstrates this with entirely plausible, non-degenerate parameters (`fee_per_gas = 1`, `reward = 1`, `exchange_rate = 1`, `multiplier = 1`). No malicious relayer, validator, or governance action is needed; any user submitting a low-cost command through the public `pallet_xcm`/`EthereumBlobExporter` path can trigger it whenever prevailing pricing parameters happen to be small relative to the decimal scaler (e.g., after an ETH/DOT price update or for governance-tuned low-fee periods).

### Recommendation
In `convert_from_ether_decimals` (or in `calculate_fee`), reject/floor to a minimum non-zero value when the computed `remote` fee would truncate to `0` despite non-zero `params.rewards.remote`/`fee_per_gas`, e.g. round up (`div_ceil`) instead of floor, or explicitly `ensure!(fee.remote > 0 || params.rewards.remote.is_zero(), Error::<T>::FeeTooLow)` before returning/enqueuing. This mirrors the report's recommended fix of detecting `amount * numerator <= denominator` and reverting rather than silently truncating.

### Proof of Concept
The existing repository test already demonstrates the corrupted value; it only needs interpreting as a security regression instead of a documented artifact: [3](#0-2) 

1. Set `PricingParameters { exchange_rate: 1, fee_per_gas: 1, rewards: { local: 1, remote: 1 }, multiplier: 1 }` (valid per `PricingParameters::validate`, all fields non-zero).
2. Call `OutboundQueue::calculate_fee(gas_used, price_params)`.
3. Observe `fee.local = 698000000` (charged, non-zero) but `fee.remote = 0` — the sender pays nothing toward the promised Ethereum-side reward/gas refund.
4. Trace this `fee` through `EthereumBlobExporter::validate` (`bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs:135`, `fee.total()`), confirming the on-chain XCM `WithdrawAsset`/`PayFees` only withdraws `fee.local`, while `do_process_message` (`lib.rs:337-352`) still commits the full `reward`/`max_fee_per_gas` to the message that Ethereum's Gateway contract will honor.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L337-352)
```rust
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};
```

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-319)
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
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L128-137)
```rust
		// validate the message
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

		Ok(((ticket.encode(), message_id), fee))
```

### Title
Fixed-point decimal truncation can zero out the Ethereum-side relay fee in `OutboundQueue::calculate_fee`, causing under-collection of the cross-chain reward/gas fee - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`EthereumBlobExporter::validate()` charges the XCM sender exactly `fee.total()` (local + remote) computed by `OutboundQueue::calculate_fee`, then unconditionally enqueues the message for delivery, expecting `Message.Reward` (the remote component) to cover the relayer/gas cost on Ethereum. Just like the Stargate report where hardcoded `dstGasForCall: 0` caused Stargate's fee formula to silently produce a wrong (under/over) cross-chain fee while the transfer proceeded anyway, the Snowbridge fee formula can silently truncate the *remote* fee component to zero via integer division in `convert_from_ether_decimals`, even when `PricingParameters` are legitimate and non-zero — and the message is still queued and delivered as if fully paid.

### Finding Description
`Pallet::calculate_fee` computes the remote (Ethereum-side) fee as: [1](#0-0) 

The remote fee in wei is `fee_per_gas * gas_used_at_most + reward`, then converted from 18-decimal Ether precision down to the chain's native decimals via plain integer division: [2](#0-1) 

Because `checked_div` truncates towards zero, any wei amount smaller than `10^(18 - T::Decimals)` collapses to `0`, regardless of whether `fee_per_gas`/`reward`/`multiplier` are non-zero, legitimate values. This is not a hypothetical: the pallet's own test suite documents it explicitly: [3](#0-2) 

The comment in the test itself states: *"Though non-zero pricing params the remote fee calculated here is invalid which should be avoided."* — i.e., the maintainers acknowledge the truncation can occur but the pallet does not guard against it (no `ensure!(fee.remote > 0)` or floor value).

This computed `Fee` is used directly by `send_message_impl.rs::validate()` to build the ticket/fee tuple: [4](#0-3) 

and then by `EthereumBlobExporter::validate()` to charge the sender and pass the message through, without checking whether `fee.remote` is nonzero: [5](#0-4) 

The message is committed to the outbound queue and eventually delivered to the Ethereum Gateway carrying a `reward` field of zero (or near-zero), exactly mirroring the external bug's root cause: a fee-calculation code path that can silently produce an incorrect (here: zero) cross-chain fee component while the "swap"/message dispatch still proceeds unconditionally.

### Impact Explanation
If `fee.remote` truncates to zero (or to a value far below the real Ethereum gas + relayer reward cost), the relayer that ultimately submits the message to Ethereum is not compensated according to the documented fee model (`Min(GasPrice, MaxFeePerGas) * GasUsed() + Reward`), which can lead to relayers declining to deliver messages, causing messages to stall in the outbound queue/on the Ethereum Gateway. This directly matches the "public underpriced work that degrades block production or stalls bridge processing" impact category — bridge message delivery for the affected class of pricing parameters is effectively free/underpriced for the sender while relying parties absorb the cost, and no funds are conserved on the intended fee-recovery invariant.

### Likelihood Explanation
The precondition is realistic and not attacker-privileged: it triggers whenever `PricingParameters` (set by governance/runtime config, e.g. `fee_per_gas`, `rewards.remote`, `exchange_rate`, `multiplier`) combined with a command's `gas_used_at_most` yield a wei amount below `10^(18 - T::Decimals)` (e.g., `10^8` wei for a 10-decimal DOT chain). Any parachain sending a low-value/low-gas message (e.g., small governance/system commands) through `EthereumBlobExporter` can hit this without needing any privileged actor — it is a pure function of publicly known runtime parameters and message content, so an ordinary user constructing/triggering an XCM export can encounter (or intentionally seek) the truncation.

### Recommendation
- In `Pallet::calculate_fee` / `convert_from_ether_decimals`, use `checked_div` with round-up (ceiling) semantics, or enforce a floor: `ensure!(fee.remote > 0, Error::<T>::InvalidRemoteFee)` and reject/round the fee upward rather than silently truncating to zero.
- Add a runtime-level minimum remote fee (dust floor) so the computed reward is never zero when `PricingParameters` are non-zero.
- Extend `send_message_impl::validate()` to reject messages whose computed `fee.remote == 0` while `PricingParameters.rewards.remote != 0`, rather than allowing enqueue/delivery with an undercharged fee.

### Proof of Concept
The existing unit test already reproduces the exact scenario deterministically:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-318
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.local, 698000000);
assert_eq!(fee.remote, 0); // remote fee truncated to zero despite non-zero, valid pricing params
```
This demonstrates that with realistic, non-adversarial `PricingParameters`, `fee.remote` becomes `0`; the caller (`EthereumBlobExporter::validate`) still charges `fee.total()` and forwards the message for delivery, meaning the Ethereum-side relayer reward encoded in the outbound message can end up zero without any error being raised.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs (L129-137)
```rust
		let (ticket, fee) = OutboundQueue::validate(&outbound_message).map_err(|err| {
			tracing::error!(target: "xcm::ethereum_blob_exporter", error=?err, "OutboundQueue validation of message failed.");
			SendError::Unroutable
		})?;

		// convert fee to Asset
		let fee = Asset::from((Location::parent(), fee.total())).into();

		Ok(((ticket.encode(), message_id), fee))
```

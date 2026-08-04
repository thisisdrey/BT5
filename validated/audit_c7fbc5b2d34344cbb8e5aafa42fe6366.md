## Finding

### Title
`PricingParameters::validate()` allows non-zero fee inputs that still cause `OutboundQueue::calculate_fee` to silently compute a `remote` fee of `0`, letting bridge messages be delivered without paying the required relayer reward - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
This is a real Snowbridge analog of the GMX "virtual impact skipped, fee bypassed" bug: instead of a fee/impact calculation being *skipped* when one input is missing, the Snowbridge outbound-queue fee calculation *rounds to zero* when the (validated, non-zero) pricing parameters are small relative to the decimal-conversion divisor, letting a message be queued for Ethereum delivery while the relayer reward component of the fee is silently zero.

### Finding Description
`Pallet::calculate_fee` computes the fee owed for delivering a message to Ethereum: [1](#0-0) 

The remote-fee component is derived as:
```
fee_wei = fee_per_gas * gas_used_at_most + reward           // U256, downcast to u128
fee = FixedU128::from_inner(fee_wei) * multiplier / exchange_rate
fee = convert_from_ether_decimals(fee.into_inner())          // divides by 10^(18 - local_decimals)
```
`convert_from_ether_decimals` performs plain integer division: [2](#0-1) 

If `fee_wei * multiplier / exchange_rate` is smaller than `10^(18 - local_decimals)` (e.g. `10^6` for a 12-decimal chain), integer division truncates the whole remote fee to `0`, even though every individual parameter passed `PricingParameters::validate()`: [3](#0-2) 

`validate()` only checks that `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are individually non-zero - it never checks that the *combination* produces a remote fee that survives the decimal-conversion division. This is exactly analogous to the GMX bug: a fee/impact-defeating condition (`hasVirtualInventoryTokenA/B` false → return early) is bypassed by an input combination the guard doesn't anticipate; here the guard (`validate()`) doesn't anticipate the truncation-to-zero condition in `calculate_fee`.

The repository's own test explicitly demonstrates and flags this as a defect: [4](#0-3) 

The computed `Fee` is used directly to charge the sender in `SendMessage::validate` for every outbound message (both user-initiated XCM transfers and system commands): [5](#0-4) 

so a `remote == 0` fee is charged and accepted with no additional guard before the message is enqueued and later committed for relaying.

### Impact Explanation
The `remote` fee is the pool from which relayers are refunded gas costs and paid their reward on Ethereum (per the module docs: `RemoteFeeAdjusted = Multiplier * (RemoteFee / ExchangeRate)`). If this computes to `0` while parameters are still nominally "valid," messages are queued and committed for delivery to Ethereum with no funded relayer reward/gas refund attached. This is "public underpriced work" in the message-queue/bridge-delivery flow: senders pay only the (non-zero) local processing fee while the remote economic cost of delivery is uncompensated, which can (a) let attackers spam bridge messages cheaply, forcing relayers to either subsidize delivery or ignore messages, and (b) stall bridge processing since no rational relayer will deliver uncompensated messages, causing message backlog on BridgeHub.

### Likelihood Explanation
The trigger condition only requires `fee_per_gas`, `reward`, or the derived `fee_wei * multiplier / exchange_rate` product to fall under the `10^(18 - local_decimals)` threshold (`10^6` wei on a 12-decimal chain such as Polkadot/Kusama BridgeHub). This is entirely plausible under normal, non-malicious governance operation — e.g. gas prices dropping, exchange-rate updates, or `fee_per_gas`/`reward` being set in units the operator did not realize map to sub-`10^6`-wei absolute contribution once combined with `gas_used_at_most`. Crucially, `validate()` — the only on-chain guard invoked by `set_pricing_parameters` — passes these inputs, so there is no protection against this class of value once parameters are otherwise non-zero. Any user can then trigger the truncation deterministically by simply sending any message once such parameters are live; no privileged action is required by the exploiting party.

### Recommendation
Strengthen `PricingParameters::validate()` (or add a check inside `calculate_fee`) to reject/flag parameter combinations for which the computed `remote` fee would round to zero for the minimum expected `gas_used_at_most`, or perform the decimal conversion with rounding-up / a minimum-fee floor instead of silent truncation-to-zero division. At minimum, `calculate_fee` should return an error or use `checked_div`/ceiling division so a legitimately non-zero economic cost is never silently reported and charged as `0`.

### Proof of Concept
The existing unit test already reproduces the issue: [4](#0-3) 

With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1`, and `gas_used = 250000`:
- All parameters pass `PricingParameters::validate()`.
- `fee_wei = 1 * 250000 + 1 = 250001`.
- After multiply/divide by `multiplier`/`exchange_rate` (no-ops here) and `convert_from_ether_decimals` (divide by `10^6` for the mock's 12-decimal `Decimals`), `250001 / 1_000_000 = 0`.
- Result: `fee.local = 698000000`, `fee.remote = 0` — confirmed by the test assertion `assert_eq!(fee.remote, 0);`.

Any message sent via `SendMessage::validate` under such parameters is queued and later committed to Ethereum via `do_process_message`/`commit` with `reward: 0` embedded in the `CommittedMessage`, with no additional check preventing acceptance of the zero-reward message.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
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

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L35-56)
```rust
impl<Balance> PricingParameters<Balance>
where
	Balance: BaseArithmetic + Unsigned + Copy,
{
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
	}
```

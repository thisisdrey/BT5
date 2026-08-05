## Title
Integer-division rounding in `calculate_fee` can silently zero-out the Ethereum-side relayer reward for a fully "valid" `PricingParameters` set - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
This is a local analog of the "stale/invalid oracle price accepted without validation" pattern: instead of a Chainlink price feed being consumed without a liveness check, the Snowbridge outbound queue consumes a governance-set `PricingParameters` value without validating that the *derived* fee computation stays non-zero. `PricingParameters::validate()` only checks that individual fields (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are non-zero, but never checks the resulting `remote` fee after decimal conversion. `calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` performs a `checked_div` and an integer division (`convert_from_ether_decimals`) that can truncate a small but "valid" remote fee down to `0`, and this is directly reproduced in the pallet's own test suite.

## Finding Description
`PricingParameters::validate()` in `bridges/snowbridge/primitives/core/src/pricing.rs` only guards against zero-valued inputs: [1](#0-0) 

The actual per-message fee is computed in `Pallet::calculate_fee`: [2](#0-1) 

and [3](#0-2) 

`convert_from_ether_decimals` performs `value.checked_div(denom)`, an integer division with truncation toward zero. When `fee_per_gas`/`reward` are small relative to `10^(ETHER_DECIMALS - Decimals)` (e.g. `10^8` for a 10-decimal native currency), the resulting `remote` component of `Fee` truncates to `0`, even though every individual `PricingParameters` field passed `validate()` as strictly non-zero. This is confirmed directly by the pallet's own regression test, whose comment acknowledges the defect: [4](#0-3) 

`set_pricing_parameters` in `bridges/snowbridge/pallets/system/src/lib.rs` is the only gate on this data, and it only calls `params.validate()` before committing it to storage - it never simulates `calculate_fee` to ensure the derived remote fee stays economically meaningful: [5](#0-4) 

Every subsequent call to `OutboundQueue::validate` (the public, permissionless `SendMessage::validate` entrypoint used whenever a user routes an XCM message to Ethereum) reads this stored `PricingParameters` and calls `calculate_fee` without any additional floor check: [6](#0-5) 

The corrupted value is the `remote` field of the returned `Fee<Balance>` - it is silently rounded to `0` instead of being rejected, and no guard (`ensure!`, `Error`, or floor) exists anywhere on the accept path to catch it.

## Impact Explanation
`Fee.remote` is supposed to reserve/charge the ETH-denominated reward that funds gas reimbursement and relayer incentive for delivering the message to Ethereum (module docs describe it as covering "the gas refund paid out to relayers" and "an additional reward paid out to relayers"). If this collapses to `0`, any unprivileged user can enqueue messages to the outbound queue (`do_process_message`, `commit`) that get accepted and committed on-chain while contributing nothing toward Ethereum-side relayer compensation. This is public, underpriced work: it lets an attacker push an unbounded number of messages into the bridge queue for free on the remote-fee side, exhausting `MaxMessagesPerBlock`/relayer incentive economics and stalling bridge processing (no relayer will pick up unrewarded messages), matching the in-scope "public underpriced work that degrades block production or stalls bridge processing" impact category. It does not require a malicious relayer, validator, or governance actor - governance merely sets *plausible-looking* parameters that pass `validate()`, and the flaw is purely arithmetic/rounding in the public dispatch path.

## Likelihood Explanation
Likelihood is moderate-to-high: it requires no attacker privilege beyond calling the already-public `send`/`validate` message path once governance parameters happen to fall into the truncation range (which is entirely plausible for low-decimal-precision fee-per-gas/reward values relative to the `10^8`-scale decimal adjustment between ETH's 18 decimals and a chain's native decimals, e.g. DOT's 10). The pallet's own test file already demonstrates the exact zero-fee outcome with "valid" non-zero parameters, showing this is not a hypothetical edge case but a reproducible arithmetic property of the current code.

## Recommendation
- Add a post-computation floor check in `calculate_fee` (or in `PricingParameters::validate`, simulated against expected gas/decimal ranges) that rejects/ensures `fee.remote > 0` before returning, e.g. return `DispatchError`/`ensure!` in `set_pricing_parameters` by test-computing `calculate_fee` for the minimum expected `gas_used_at_most` and failing if `remote == 0`.
- Alternatively, use rounding-up division (`div_ceil`) in `convert_from_ether_decimals` so the remote fee never truncates to zero for non-zero inputs.

## Proof of Concept
The existing unit test already demonstrates the bug end-to-end: [4](#0-3) 

Steps to reproduce in the live system:
1. Governance calls `snowbridge_pallet_system::set_pricing_parameters` with a `PricingParameters` where `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, `exchange_rate = 1` (all non-zero, so `validate()` passes).
2. Any unprivileged user sends an XCM message that gets routed to the Snowbridge exporter, invoking `OutboundQueue::validate` → `calculate_fee`.
3. `calculate_remote_fee` yields a small positive `u128` (e.g. `250001` wei-equivalent units), but `convert_from_ether_decimals`'s integer division by `10^decimals` truncates it to `0`.
4. The message is queued and eventually committed/dispatched to Ethereum with `Fee.remote == 0`, i.e., zero relayer reward reserved, while `Fee.local` is still charged - the remote economic backing for delivering the message is silently dropped.

### Citations

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L315-334)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());

			let command = Command::SetPricingParameters {
				exchange_rate: params.exchange_rate.into(),
				delivery_cost: T::InboundDeliveryCost::get().saturated_into::<u128>(),
				multiplier: params.multiplier.into(),
			};
			Self::send(PRIMARY_GOVERNANCE_CHANNEL, command, PaysFee::<T>::No)?;

			Self::deposit_event(Event::PricingParametersChanged { params });
			Ok(())
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

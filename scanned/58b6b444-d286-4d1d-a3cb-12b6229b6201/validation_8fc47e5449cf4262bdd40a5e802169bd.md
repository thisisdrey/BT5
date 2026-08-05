### Title
Fee-decimal conversion rounds Ethereum delivery fee down to zero, letting senders underpay the Snowbridge relayer/gas fee - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::calculate_fee` in the Snowbridge outbound-queue pallet converts the Ethereum-side cost of a message (gas + relayer reward, denominated in 18-decimal wei) into the local chain's balance decimals via `convert_from_ether_decimals`, which performs a plain integer division (`checked_div`) with no minimum/round-up guard. When the local chain has few decimals relative to Ether (e.g. 10-12 decimals on Polkadot/Kusama BridgeHub) and the wei-denominated `remote fee` is small relative to the resulting divisor, the division truncates to zero, exactly mirroring the reported `FarmKeeper` bug where `amount * SCALE_FACTOR / liquidity` rounds down to 0 due to a scale factor mismatched to the token's decimal precision. [1](#0-0) 

### Finding Description
`calculate_fee` computes the remote (Ethereum) fee in wei, downcasts to `u128`, wraps it in `FixedU128::from_inner`, applies `multiplier`/`exchange_rate`, then calls `convert_from_ether_decimals`, which does:
```
let denom = 10u128.saturating_pow(decimals); // decimals = 18 - T::Decimals
value.checked_div(denom).expect(...)
``` [2](#0-1) 

This is invoked from the public, unprivileged `SendMessage::validate` entrypoint every time any user (or sibling parachain via XCM) submits a message to be bridged to Ethereum: [3](#0-2) 

The pallet's own test suite demonstrates the rounding-to-zero directly: with `gas_used=250000`, `fee_per_gas=1`, `reward=1`, `exchange_rate=1`, `multiplier=1`, and a 12-decimal chain, `fee.local` is non-zero (698000000) while `fee.remote` — the component meant to cover Ethereum gas + relayer reward — is exactly `0`: [4](#0-3) 

This is structurally identical to the reported bug class: a fixed scaling/decimal-conversion factor (`ETHER_DECIMALS - T::Decimals`, analogous to `SCALE_FACTOR`) is applied via floor-division against values whose magnitude can be small relative to the divisor (here, low `fee_per_gas`/`reward` pricing parameters relative to `10^(18-Decimals)`), causing the computed remote fee to collapse to `0` instead of a small positive amount. Unlike the AMM report where liquidity is attacker-uncontrolled, here `PricingParameters` (`fee_per_gas`, `rewards.remote`, `exchange_rate`, `multiplier`) are governance-set values that legitimately change over time (per module docs, they must be periodically updated to track ETH/DOT exchange rates), so any parameter update that lands in the "small relative to `10^(18-Decimals)`" regime silently zeroes out the remote-fee charge for every message sent thereafter, with no validation or floor check anywhere in `calculate_fee`/`convert_from_ether_decimals`/`PricingParameters::validate` guarding against this outcome (`validate()` only checks that each parameter individually is non-zero, not that their combined product survives the decimal conversion): [5](#0-4) 

### Impact Explanation
The pallet's own documentation states the fee is meant to cover "the gas refund paid out to relayers" and "an additional reward paid out to relayers for message submission." When `fee.remote` rounds to zero, senders are charged nothing for this component while the outbound message still embeds the full intended reward in wei (`pricing_params.rewards.remote`) in the `CommittedMessage` sent to Ethereum: [6](#0-5) 

This creates a systemic underpricing/free-riding condition on a public dispatch path: every user submitting a bridged message escapes the remote-cost charge that is supposed to fund relayer incentives, depleting the funding source that keeps relayers economically motivated to service the bridge, which matches the "public underpriced work that degrades block production or stalls bridge processing" impact class. It is not a one-off griefing bet but a durable state (persists until governance changes `PricingParameters` again), silently draining protocol economics rather than any single actor's funds.

### Likelihood Explanation
Likelihood is Medium: this requires `PricingParameters` (an ordinary governance-updatable value, not attacker-controlled) to fall into a regime where the wei-denominated remote fee is small relative to `10^(18 - T::Decimals)`. Because the module documentation explicitly calls for periodic governance updates of `fee_per_gas`/exchange rate, and no code path checks that the resulting `fee.remote` is non-zero (or rounds up), an update landing in this regime is a realistic operational error rather than a purely theoretical one — as proven by the pallet's own regression test exercising exactly this scenario.

### Recommendation
- In `convert_from_ether_decimals`, use `multiply_by_rational_with_rounding` (already present in `sp_arithmetic::helpers_128bit`) with `Rounding::Up` instead of a raw `checked_div`, so the fee never truncates to a smaller value than the true remote cost.
- Add an explicit invariant check (either in `PricingParameters::validate` or in `calculate_fee`) that the computed `fee.remote` is non-zero whenever the pre-conversion wei amount is non-zero, returning an error/defensive-fallback rather than silently charging 0.
- Consider enforcing a floor value (e.g., 1 unit of local balance) for `fee.remote` whenever gas usage and rewards are non-zero.

### Proof of Concept
Existing repository test reproduces the exact rounding-to-zero condition: [4](#0-3) 
With `gas_used_at_most = 250_000`, `fee_per_gas = 1`, `rewards.remote = 1`, `exchange_rate = 1/1`, `multiplier = 1/1`, and chain `Decimals = 12` (as configured in the mock runtime), `calculate_fee` returns `Fee { local: 698000000, remote: 0 }`. Any sender calling `validate()` under these (governance-set) pricing parameters is charged zero for the Ethereum-side cost/reward component while the full reward is still promised to relayers on-chain via the `CommittedMessage.reward` field, demonstrating the underpriced-work path end to end. [7](#0-6)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L332-352)
```rust
			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-418)
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}

		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/mock.rs (L71-96)
```rust
parameter_types! {
	pub const OwnParaId: ParaId = ParaId::new(1013);
	pub Parameters: PricingParameters<u128> = PricingParameters {
		exchange_rate: FixedU128::from_rational(1, 400),
		fee_per_gas: gwei(20),
		rewards: Rewards { local: DOT, remote: meth(1) },
		multiplier: FixedU128::from_rational(4, 3),
	};
}

pub const DOT: u128 = 10_000_000_000;

impl crate::Config for Test {
	type RuntimeEvent = RuntimeEvent;
	type Hashing = Keccak256;
	type MessageQueue = MessageQueue;
	type Decimals = ConstU8<12>;
	type MaxMessagePayloadSize = ConstU32<1024>;
	type MaxMessagesPerBlock = ConstU32<20>;
	type GasMeter = ConstantGasMeter;
	type Balance = u128;
	type PricingParameters = Parameters;
	type Channels = Everything;
	type WeightToFee = IdentityFee<u128>;
	type WeightInfo = ();
}
```

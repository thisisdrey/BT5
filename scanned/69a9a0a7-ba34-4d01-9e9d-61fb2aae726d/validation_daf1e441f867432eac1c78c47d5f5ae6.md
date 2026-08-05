## Analysis

Reducing the external SafeMath report to its core invariant: **arithmetic helper functions used in critical accounting paths can silently produce wrong results (e.g. truncate to zero) under valid-looking inputs, and the surrounding code has no test or runtime guard that rejects the corrupted zero/degenerate result before it is committed to state.** The Notional maintainers even manually accepted the risk ("won't fix") rather than adding an invariant check.

The closest local analog is in the Snowbridge outbound queue's fee-calculation pipeline, `Pallet::calculate_fee` / `convert_from_ether_decimals`, which is invoked from the public `SendMessage::validate` entrypoint used by every outbound message (governance and user/XCM-originated) before it is enqueued.### Title
Unguarded integer truncation in outbound-queue fee derivation can zero out the remote (Ethereum) fee component while the full-value message and reward commitment still get queued for delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`Pallet::calculate_fee` (invoked from the public `SendMessage::validate` entrypoint used by every outbound message) converts a gas/reward cost denominated in wei into the local chain currency through a chain of `FixedU128` operations and a final integer division in `convert_from_ether_decimals`. There is no invariant check ensuring the resulting `remote` fee component is non-zero or otherwise sufficient to cover the reward the message actually promises the relayer. The pallet's own test suite documents this exact failure mode and explicitly states the result "should be avoided," yet ships without a guard, mirroring the SafeMath report's core problem: an arithmetic routine with unverified edge-case behavior sitting directly in a value-accounting path.

### Finding Description
`calculate_fee` at [1](#0-0)  computes:
1. `calculate_remote_fee` — a wei-denominated `U256` = `fee_per_gas * gas_used_at_most + reward`.
2. Downcast to `u128`, wrapped via `FixedU128::from_inner`, multiplied by `multiplier`, divided by `exchange_rate`.
3. `convert_from_ether_decimals` — a final `checked_div` by `10^(ETHER_DECIMALS - T::Decimals)` at [2](#0-1) .

Because native chains (DOT: 10 decimals, KSM: 12 decimals) have far fewer decimal places than ETH (18), step 3 performs integer division by `10^8` (DOT) or `10^6` (KSM). Any true fee value smaller than that denominator floors to `0` via `checked_div`, and the function returns `Fee::from((local_fee, 0))` with no error, no `ensure!`, and no defensive check. This is validated directly by the pallet's own unit test, which computes a non-zero-parameter fee and gets `fee.remote == 0`, with an inline comment flagging it as invalid: [3](#0-2) 

Critically, `calculate_fee`'s output is only used to charge the *sender* the local-currency fee in `SendMessage::validate`, at [4](#0-3) . It is **not** the same value later committed on-chain as the wei-denominated reward promised to the relayer in `do_process_message`: [5](#0-4) 
There, `reward: reward.try_into().defensive_unwrap_or(u128::MAX)` uses `pricing_params.rewards.remote` directly in wei — a fixed, real Ethereum-side obligation — independent of whatever local-currency amount was actually collected from the sender. In other words: the chain can under-collect (down to exactly `0`) the local-currency cost meant to back a message's ETH-side reward while still queuing a message that carries the full, real reward commitment to be paid out on Ethereum.

`gas_used_at_most` is derived from `T::GasMeter::maximum_gas_used_at_most(&message.command)`, which varies with the command an unprivileged sender submits (e.g. minimal-gas commands via XCM `ExportMessage`/asset transfers routed through the exporter). By choosing low-gas commands, or simply operating under governance-set pricing parameters where the ETH-decimal-to-local-decimal conversion factor dominates the true cost magnitude, an ordinary user can drive the truncation to zero without needing to influence `PricingParameters` themselves (those remain validated as non-zero individually by `PricingParameters::validate`, which checks the *inputs*, not the *derived output*): [6](#0-5) 

`PricingParameters::validate` guards `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` individually being non-zero, but does nothing to prevent the *composed* `calculate_fee` result from truncating to zero for legitimately-configured, realistic parameters combined with attacker-chosen low-gas messages. This is precisely the "custom-math function with no unit tests / no invariant check on the composed result" pattern from the external report.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" from the impact gate: an unprivileged actor can submit outbound messages that are undercharged for their true remote-delivery cost (down to zero), while `do_process_message` still queues the message with the full wei-denominated reward commitment for relayers. Over time this creates a shortfall between fees actually collected from senders and the funds needed to back relayer rewards, degrading the bridge's economic sustainability — relayers are disincentivized from delivering underfunded messages, causing message backlog/stalling in `MessageLeaves`/`Messages` storage, which is exactly the "stalls bridge processing" outcome called out in the impact gate.

### Likelihood Explanation
Medium. Triggering the truncation does not require a malicious peer, validator, relayer, or governance actor — only an ordinary sender constructing a low-gas-cost outbound command under governance-configured (but otherwise legitimate) `PricingParameters`. The pallet's own test explicitly reproduces the zero-fee outcome with valid, non-zero parameters, confirming the code path is reachable, not merely theoretical. The main uncertainty is how tightly real-world pricing parameters bound `gas_used_at_most * fee_per_gas + reward` relative to the `10^8`/`10^6` divisor in practice; I could not verify live parameter values from the index, so the exact frequency of zero-fee occurrences on a production BridgeHub cannot be confirmed here.

### Recommendation
Add an explicit invariant check in `calculate_fee` (or in `SendMessage::validate`) that rejects/errors when the computed `remote` fee component is `0` (or below some minimum threshold sufficient to cover the promised on-chain reward), rather than silently returning `Fee::from((local, 0))`. Additionally, add unit tests asserting `calculate_fee` never returns a zero `remote` component for any valid, non-zero `PricingParameters` combined with the full range of supported `gas_used_at_most` values, closing exactly the kind of untested arithmetic-edge-case gap the external SafeMath report warns about.

### Proof of Concept
The existing test already demonstrates the corrupted value with non-zero, "valid" pricing parameters: [3](#0-2) 
```rust
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.remote, 0); // sender charged 0 for the remote component
```
Meanwhile `do_process_message` still commits `reward: reward.try_into().defensive_unwrap_or(u128::MAX)` (using `pricing_params.rewards.remote` in wei) into the `CommittedMessage`, i.e., the real Ethereum-side reward obligation is unaffected by the zero local-currency collection: [7](#0-6)

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L414-418)
```rust
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

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L35-57)
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
}
```

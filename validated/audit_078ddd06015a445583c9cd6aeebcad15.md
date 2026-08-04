### Title
Integer-division truncation in `convert_from_ether_decimals` lets the Snowbridge outbound-queue fee collapse to zero, allowing unpriced message spam - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Chainlink report's core defect is that a bounded/clamped oracle value is trusted and used as-is without validating it is still a meaningful, in-range number, letting stale/incorrect data silently pass through. The Snowbridge outbound-queue fee pricing pipeline has the same broken invariant: the computed remote fee is silently floored to `0` by integer division when converting Ether's 18-decimal fee into the local chain's lower-precision `Balance`, and this un-validated, possibly-zero value is accepted as the "delivery fee" charged to any unprivileged user submitting a message to Ethereum.

### Finding Description
`Pallet::calculate_fee` computes a wei-denominated remote fee and converts it to local currency via `convert_from_ether_decimals`: [1](#0-0) 

```rust
pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
    let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
    let denom = 10u128.saturating_pow(decimals);
    value.checked_div(denom).expect("divisor is non-zero; qed").into()
}
```

For DOT (`Decimals = 10`), `denom = 10^8`. Any wei-scaled fee value smaller than `10^8` truncates to `0` due to plain integer division — there is no rounding-up, no minimum-fee floor, and no post-computation assertion that the resulting fee is non-zero or otherwise "in bounds". This mirrors the Chainlink issue exactly: a value passes through a lossy transformation (circuit-breaker clamp there, integer-division floor here) and the caller accepts the transformed value without checking it against the expected valid range.

The pallet's own test suite already demonstrates this defect occurring with realistic, non-zero pricing parameters: [2](#0-1) 

The comment "Though none zero pricing params the remote fee calculated here is invalid which should be avoided" is an explicit acknowledgment in-repo that the code can produce an invalid (zero) fee under valid configuration — but no `require`/`ensure` guard was added to `calculate_fee` or `convert_from_ether_decimals` to reject or correct this case, unlike the recommended Chainlink fix (`require(answer > MIN_ANSWER && answer < MAX_ANSWER)`).

This fee is not merely informational: it is the value returned from `SendMessage::validate`, used by any unprivileged caller (via XCM `ExportMessage`/`InitiateAssetsTransfer` to Ethereum) to price message delivery: [3](#0-2) 

`validate` computes `fee` from attacker-influenced inputs (`message.command`, which determines `gas_used_at_most` via `T::GasMeter::maximum_gas_used_at_most`) and hands it back to the router/exporter to charge the user, then unconditionally allows `deliver` to enqueue the message for processing and eventual relaying to Ethereum: [4](#0-3) 

Because `deliver` never re-checks that the fee computed in `validate` was non-zero/sufficient, an unprivileged user can construct commands whose `gas_used_at_most * fee_per_gas + reward` (in wei) falls under the `10^8`-wei truncation threshold for the current `exchange_rate`/`multiplier` parameters, causing `Fee.remote` (and potentially the whole remote-fee component) to be `0`, while the message is still fully accepted, queued, committed into the Merkle root, and forwarded for execution/relay on Ethereum.

### Impact Explanation
This is public underpriced work with direct chain/bridge impact: a message that requires real relayer gas expenditure and bridge processing on Ethereum can be submitted for free (zero remote fee), letting an attacker spam the outbound queue and the Ethereum gateway without paying the intended cost. This can degrade bridge throughput, exhaust relayer incentives (since `reward` sent to Ethereum can also legitimately be non-zero on-chain while the fee charged on Polkadot is zero due to truncation, or vice versa depending on parameter selection), and stalls/burdens bridge message processing — matching the "public underpriced work that degrades block production or stalls bridge processing" impact class. No malicious relayer, validator, or governance actor is required; the attacker is an ordinary user choosing message parameters.

### Likelihood Explanation
Likelihood depends on the currently configured `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards.remote`, `multiplier`) making the wei-fee-per-message land under the `10^8` (DOT) or `10^6` (KSM, 12 decimals) truncation threshold. `PricingParameters::validate()` only checks that fields are non-zero, not that the effective converted fee for realistic `gas_used_at_most` values stays above the truncation boundary: [5](#0-4) 

Because the pallet's own test (`test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`) shows this occurring with plausible, valid parameters, the condition is realistically reachable rather than purely theoretical, though it requires a specific parameter regime (hence rated Low likelihood, matching the report's own severity rating).

### Recommendation
Add an explicit bound/validity check after fee conversion, analogous to the suggested Chainlink fix, e.g. in `calculate_fee`/`convert_from_ether_decimals`:
```rust
let fee = Self::convert_from_ether_decimals(fee);
ensure!(fee > T::Balance::zero(), Error::<T>::FeeTooLow); // or saturate to a configured minimum fee
```
Alternatively, round the division up (`ceil` instead of `floor`) or enforce a `MinimumRemoteFee` constant so the computed remote fee can never truncate to zero regardless of `PricingParameters`, and extend `PricingParameters::validate()` to reject parameter combinations that produce a zero fee for the pallet's minimum supported `gas_used_at_most`.

### Proof of Concept
1. Configure (or observe already-configured) `PricingParameters` such that `fee_per_gas * gas_used_at_most + reward` (in wei) is small relative to the `exchange_rate`/`multiplier`, as demonstrated by the existing unit test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in `bridges/snowbridge/pallets/outbound-queue/src/test.rs` (lines 303-319), which reproduces `fee.remote == 0` with non-zero, `validate()`-passing pricing parameters.
2. Submit an XCM message from any parachain that routes through `SnowbridgeExporter`/`EthereumBlobExporter` to Ethereum, using a `Command` whose gas usage (`T::GasMeter::maximum_gas_used_at_most`) falls in this zero-truncation range.
3. `Pallet::validate` (in `send_message_impl.rs`) returns a `Fee` with `remote == 0`; the router charges the attacker only for the (potentially minimal) local fee.
4. `Pallet::deliver` unconditionally enqueues the message; it is processed, committed to the Merkle root, and relayed to the Ethereum Gateway for execution, with the bridge/relayer effectively unpaid for the remote-side cost, enabling repeatable free spam of bridge delivery capacity.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-88)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
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

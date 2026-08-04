## Title
Integer-truncation and periodically-updated exchange rate in Snowbridge outbound-queue fee calculation allow zero/underpriced relayer rewards, stalling bridge message delivery — (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
Tellor's bug class is: a security-critical numeric parameter (data feed price) is refreshed too infrequently and with too little economic backing, making it cheap to keep stale/inconsistent with the real market, which lets an attacker exploit the resulting mispricing. The Snowbridge outbound-queue fee mechanism has the same structural weakness: the `ETH/DOT` `exchange_rate` used to convert the Ethereum-side relayer reward into the local fee charged to users is a single governance value, explicitly refreshed only "every few weeks" [1](#0-0) , and the arithmetic that performs this conversion truncates to zero for a demonstrated, valid, non-zero parameter set [2](#0-1) . This lets messages be enqueued for Ethereum delivery whose committed relayer reward is effectively worthless, so no rational relayer will deliver them, while the user has already paid (and the protocol already collected) the local fee.

## Finding Description
`Pallet::calculate_fee` computes the total fee for an outbound message as a local component and a remote (Ethereum-side) component derived from `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards`, `multiplier`) [3](#0-2) :

```
fee = fee_per_gas * gas_used_at_most + reward         // in wei (U256)
fee = FixedU128::from_inner(fee)                        // reinterpret wei as an 18-decimal fixed value
      .saturating_mul(multiplier)
      .checked_div(exchange_rate)                       // convert ETH -> local currency
      .into_inner()
fee = convert_from_ether_decimals(fee)                  // rescale from 18 decimals to T::Decimals
```

`convert_from_ether_decimals` performs plain integer division by `10^(18 - T::Decimals)` [4](#0-3) . This is a hard truncation with no rounding-up or minimum-fee floor. The pallet's own test suite documents that with a fully valid, non-zero `PricingParameters` set (`exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {1, 1}`, `multiplier = 1`), the resulting `remote` fee computed by `calculate_fee` is exactly `0`, and the test comment explicitly flags this as an outcome that "should be avoided" [2](#0-1) .

`validate()` (called from `SendMessage::validate`, used by both `EthereumBlobExporter::validate` for XCM-routed messages and by `snowbridge_pallet_system::Pallet::send`) accepts whatever `Fee` `calculate_fee` returns without any lower bound check, then enqueues the message and lets the fee (including a possibly-zero `remote` component) be charged to the user via XCM `BuyExecution`/`PayFees` [5](#0-4) , [6](#0-5) . There is no re-check downstream in `process_message_impl.rs`/`do_process_message` that rejects messages whose embedded relayer reward is zero or below a viable relaying cost.

The root cause mirrors the report's core invariant break: the on-chain "price feed" (`exchange_rate`) is coarse and slow to update ("every few weeks", per the module docs) and the conversion pipeline built on top of it has no floor/guard, so mispriced (here: zero-priced) work can be pushed on-chain cheaply — exactly analogous to Tellor's low-frequency, cheap-to-stale price feed enabling underpriced settlement.

## Impact Explanation
A message committed to the outbound queue with `Fee.remote == 0` (or negligible) offers no economically rational incentive for an off-chain relayer to submit it to the Ethereum Gateway contract, since relayers are refunded `Min(GasPrice, MaxFeePerGas) * GasUsed + Message.Reward` on the Ethereum side [7](#0-6) . Such messages will sit in the committed `Messages`/Merkle-root state indefinitely — degrading/stalling bridge processing and effectively locking the outcome the user paid for (they already paid the local fee, and any bridged funds/commands remain undelivered). This matches the required impact class "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund or bridge-state lock."

## Likelihood Explanation
No privileged actor is required: any unprivileged parachain user constructing a normal cross-chain XCM transfer routed through `EthereumBlobExporter` triggers `calculate_fee` with the live, governance-set `PricingParameters`. Because the truncation is a pure integer-division artifact of `convert_from_ether_decimals` combined with the coarse-grained, infrequently-updated `exchange_rate`, there exist reachable parameter/gas combinations (as proven by the pallet's own regression test) where the computed remote fee rounds to zero even though every configured parameter individually passes `PricingParameters::validate()` (which only checks that each field is non-zero, not that the computed fee is non-zero) [8](#0-7) .

## Recommendation
- Enforce a minimum non-zero `remote` fee (and ideally round up rather than truncate) in `calculate_fee`/`convert_from_ether_decimals`, and reject/adjust messages whose computed remote fee is below the actual cost of relaying.
- Extend `PricingParameters::validate` (or add a runtime check in `calculate_fee`) to assert the *computed* fee is non-zero/economically viable, not just that the raw input parameters are non-zero.
- Consider tightening the staleness window for `exchange_rate` updates or sourcing it from a live on-chain price mechanism, consistent with the module's own stated plan to move to DEX-based pricing.

## Proof of Concept
The existing unit test in the repository already demonstrates the underlying defect: [2](#0-1) 

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

Any message validated/enqueued under such a parameter regime (achievable whenever governance-configured `PricingParameters` combined with a message's `gas_used_at_most` yield a small wei amount relative to `10^(18 - T::Decimals)`) will be committed with `remote == 0`, be charged to the user, and then have no viable relayer incentive on Ethereum — reproducing the "cheap-to-break/underpriced-oracle-driven-settlement" pattern from the external Tellor report inside this repository's bridge fee pipeline.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-58)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L72-80)
```rust
//! ## Fee Settlement
//!
//! On the remote side, in the gateway contract, the relayer accrues
//!
//! ```text
//! Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward
//! ```
//! Or in plain english, relayers are refunded for gas consumption, using a
//! price that is a minimum of the actual gas price, or `Message.MaxFeePerGas`.
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

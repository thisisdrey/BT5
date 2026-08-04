### Title
`calculate_fee` in Snowbridge outbound-queue can round the Ethereum-side remote fee to zero, allowing underpriced/free message dispatch to Ethereum - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The GMX report's core flaw is: code relies on an unenforced assumption (a bounded ratio) that does not hold in all cases, and no explicit check exists to guarantee the invariant. The local analog is `Pallet::calculate_fee` / `calculate_remote_fee` in the Snowbridge `outbound-queue` pallet: the pallet's documentation and fee-computation design assume the remote (Ethereum-side) fee will always be a meaningful non-zero value that "covers the gas refund and additional reward" for relayers, but with legitimate, non-zero `PricingParameters` this can compute to exactly `0`, and there is no explicit minimum-fee check anywhere in `validate`/`calculate_fee` to prevent this.

### Finding Description
`calculate_fee` computes the remote (Ethereum) fee as: [1](#0-0) 

and `calculate_remote_fee`: [2](#0-1) 

The module doc states the fee mechanism is designed so that "an upfront fee must be paid for delivering a message," covering local processing weight, the relayer gas refund, and an additional relayer reward, with a `Multiplier` safety factor to protect against ETH/DOT exchange-rate fluctuations: [3](#0-2) 

This is exactly the kind of implicit, unverified invariant the GMX report criticizes: the code (and its comments) assume the computed remote fee will be non-zero/adequate, but the arithmetic chain — `U256` multiplication/addition, `try_into` downcast to `u128`, `FixedU128` division by `exchange_rate`, and finally `checked_div` by a decimal-adjustment `denom` in `convert_from_ether_decimals` — can truncate a small nonzero fee down to `0` for legitimate parameter combinations (small `fee_per_gas`/`reward`, integer division/decimal truncation). This is demonstrated by the pallet's own test: [4](#0-3) 

The test's comment — "Though none zero pricing params the remote fee calculated here is invalid which should be avoided" — is a direct acknowledgment that the assumption ("fee will always be meaningfully non-zero") does not hold, mirroring the GMX finding where "a maximum of ~7%... would be added" did not hold in all cases. Crucially, no explicit check enforces a minimum remote fee anywhere in the call path. `SendMessage::validate` calls `calculate_fee` and returns whatever value results, with no floor/assertion: [5](#0-4) 

Because the returned `Fee { local, remote }` is what callers (e.g. XCM exporters charging the user) use to determine how much the sender must pay for the remote/Ethereum leg, a `remote` fee of `0` means the message is dispatched to Ethereum without paying for the relayer's gas refund or reward component defined in the module's own fee-settlement model: [6](#0-5) 

### Impact Explanation
This falls under the "public underpriced work that degrades block production or stalls bridge processing" impact category. If governance sets (or later re-sets) `PricingParameters` such that `calculate_remote_fee`'s output, after currency conversion, rounds down to `0` (which is not a config-abuse scenario but simply a consequence of normal integer/decimal truncation for certain valid parameter magnitudes), any unprivileged user can submit messages to be relayed to Ethereum while paying nothing for the remote leg. This removes the economic backstop that funds relayer gas refunds and rewards, letting users push message volume through the bridge's outbound queue for free on the Ethereum side — degrading bridge processing economics and potentially causing relayers to stop servicing the channel (denial of service on the bridge egress path) while the chain still consumes local weight/PoV for enqueuing and committing these messages.

### Likelihood Explanation
Likelihood is not attacker-controlled directly (parameters are governance-set), but the flaw is a latent, provable arithmetic gap reachable purely through normal, permitted parameter values — no malicious governance action, admin abuse, or privileged actor is required to trigger the bug; the vulnerability is in the missing invariant enforcement itself, exactly as in the GMX case (the ratio wasn't violated by an attacker maliciously configuring GMX, but by ordinary usage patterns). Given decimals differ across chains (10 vs 12) and `fee_per_gas`/`reward` values are ordinary `U256` integers subject to routine repricing (as seen in the migration code adjusting `fee_per_gas` by a percentage), zero-rounding is a realistic operational occurrence, not a contrived edge case.

### Recommendation
Do not assume `calculate_remote_fee`/`calculate_fee` will always yield a usable non-zero value. Add an explicit minimum-fee check (e.g., `ensure!(fee.remote > MinimumRemoteFee, Error::<T>::FeeTooLow)` or return an error from `validate`) so that messages cannot be queued when the computed remote fee would fail to cover the documented gas-refund/reward guarantee. Remove reliance on the implicit assumption and enforce the invariant in code, consistent with the GMX resolution to stop relying on an unstated bound and instead add an explicit check.

### Proof of Concept
The pallet's own unit test demonstrates the exact scenario:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-318
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
With `fee_per_gas = 1`, `reward.remote = 1`, and `gas_used = 250000`, `calculate_remote_fee` yields `250001` (wei-scale), which after `FixedU128` conversion and `convert_from_ether_decimals`'s decimal-truncating division ends up as `fee.remote == 0` while `fee.local` remains non-zero — proving a fully valid, non-malicious parameter set produces a zero remote fee, with `validate` in `send_message_impl.rs` returning this `Fee` unchecked to the caller and allowing message enqueue. [7](#0-6)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-70)
```rust
//! # Fees
//!
//! An upfront fee must be paid for delivering a message. This fee covers several
//! components:
//! 1. The weight of processing the message locally
//! 2. The gas refund paid out to relayers for message submission
//! 3. An additional reward paid out to relayers for message submission
//!
//! Messages are weighed to determine the maximum amount of gas they could
//! consume on Ethereum. Using this upper bound, a final fee can be calculated.
//!
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
//!
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
//!
//! By design, the computed fee includes a safety factor (the `Multiplier`) to cover
//! unfavourable fluctuations in the ETH/DOT exchange rate.
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L395-402)
```rust
		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
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

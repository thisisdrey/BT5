Audit Report

## Title
Unchecked zero-value remote fee lets users underpay for Ethereum-bound bridge messages - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::calculate_fee` computes the remote (Ethereum-side) fee component through a chain of truncating integer operations (`checked_div` in fixed-point space followed by `convert_from_ether_decimals`), and this chain can legitimately produce `fee.remote == 0` even when all `PricingParameters` fields pass `validate()`. No check exists in `calculate_fee`, `send_message_impl::validate`, or `do_process_message` to reject or reprice a zero result, while the actual relayer reward committed on-chain (`pricing_params.rewards.remote`) is taken independently and is always non-zero, decoupling what the user is charged from what the bridge owes the relayer.

## Finding Description
`calculate_fee` computes `calculate_remote_fee` (wei), downcasts to `u128`, multiplies by `multiplier`, divides by `exchange_rate` via `FixedU128::checked_div`, then rescales decimals via `convert_from_ether_decimals`. [1](#0-0) 
`PricingParameters::validate()` only checks that inputs are non-zero, never that the computed fee output is non-zero. [2](#0-1) 
`send_message_impl::validate` calls `calculate_fee` and returns the resulting `Fee` unchecked to the caller as the price to charge. [3](#0-2) 
Separately, `do_process_message` embeds the fixed, non-zero `pricing_params.rewards.remote` as the committed relayer reward, independent of what `calculate_fee` produced for the sender. [4](#0-3) 
The pallet's own test confirms a zero remote fee is reachable with plausible non-zero parameters (`exchange_rate=1`, `fee_per_gas=1`, `reward=1`, `multiplier=1`), explicitly noting in a comment that this is a known, unaddressed defect. [5](#0-4) 

## Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category: an unprivileged account calling into the outbound queue (e.g., via XCM export to Ethereum) can obtain `fee.remote == 0` for legitimate, low-gas commands under governance-set pricing parameters, while the bridge still commits a full, non-zero relayer reward obligation on the Ethereum side. Repeated submission of such messages allows a user to impose real relayer-servicing costs on the bridge while paying nothing for the remote-fee component, without needing governance, relayer, or validator compromise.

## Likelihood Explanation
The truncation is inherent to the integer-division chain (`checked_div` + `convert_from_ether_decimals`) and is demonstrated directly in the pallet's existing test suite with unremarkable parameter values, not a contrived edge case. Since `validate()` only checks input non-zero-ness and not output non-zero-ness, any combination of governance-set `fee_per_gas`/`exchange_rate`/`multiplier` together with a low `gas_used_at_most` for a given command can produce this outcome, and an attacker only needs to identify and repeatedly trigger such a command—no privileged access is required to exploit it once such a parameter/gas combination exists.

## Recommendation
In `calculate_fee`, explicitly check that `fee.remote` (and defensively `fee.local`) is non-zero whenever the pre-conversion `calculate_remote_fee` result was non-zero. On detecting truncation to zero, either use ceiling division instead of floor division, or reject/clamp to a minimum non-zero charge, mirroring the input-validation pattern already present in `PricingParameters::validate()` but applied to the computed fee output.

## Proof of Concept
The existing unmodified repository test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` demonstrates the bug: with `exchange_rate=1/1`, `fee_per_gas=1`, `rewards={local:1, remote:1}`, `multiplier=1/1`, and `gas_used=250000`, `calculate_fee` returns `fee.local=698000000` and `fee.remote=0`, i.e., the message is fully valid and dispatched with a real relayer reward commitment while the user pays zero for the remote-fee component. [5](#0-4)

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-73)
```rust
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

## Analysis

The Sherlock report's core defect is: **a price/value returned by a calculation can be zero, and the code path that consumes that value has no zero-check, silently treating a broken computation as valid.** The direct structural analog in this repository is in the Snowbridge outbound-queue fee pricing pipeline. [1](#0-0) 

### Title
Unchecked zero-value remote fee lets users underpay for Ethereum-bound bridge messages - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the remote (Ethereum-side) component of a message's delivery fee via fixed-point multiplication and integer division/truncation, then converts decimals with an additional integer division in `convert_from_ether_decimals`. This chain of truncating operations can legitimately produce `fee.remote == 0` even when all `PricingParameters` fields pass `validate()` (i.e., are non-zero). No check exists anywhere in `calculate_fee`, `send_message_impl::validate`, or any caller to reject or reprice a zero result, mirroring exactly the unchecked-zero-price flaw in the Oracle report.

### Finding Description
`calculate_fee` computes: [2](#0-1) 

1. `calculate_remote_fee` produces `fee_per_gas * gas_used + reward` in wei (U256).
2. This is downcast to `u128`, multiplied by `multiplier`, divided by `exchange_rate` using `FixedU128::checked_div`.
3. `convert_from_ether_decimals` performs another integer division by `10^decimals` to rescale from 18-decimal ether units to the local currency's decimals (10 or 12).

This is a chain of lossy integer operations. The pallet's own test suite proves the zero outcome is reachable with **valid, non-zero** pricing parameters: [3](#0-2) 

The comment in the test itself states: *"Though non-zero pricing params the remote fee calculated here is invalid which should be avoided"* — i.e., this is a known-but-unfixed defect, not an edge case the authors were unaware of.

`PricingParameters::validate()`, called in `set_pricing_parameters`, only checks that the *inputs* (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are non-zero: [4](#0-3) 

It never validates the *output* of `calculate_fee`. The computed `Fee { local, remote }` then flows unchecked out of `send_message_impl::validate`: [5](#0-4) 

Meanwhile, the actual reward promised to the relayer on the Ethereum side is **not** derived from this fee calculation — it is the raw governance-set `pricing_params.rewards.remote`, embedded directly into the committed message during `do_process_message`: [6](#0-5) 

So the two values are decoupled: the fee charged to the message sender (which can silently round to `0`) and the reward obligation encoded on-chain for the relayer (which is always the fixed non-zero `rewards.remote`) are computed independently, with no guard tying them together or rejecting a zero-charge outcome.

### Impact Explanation
Any unprivileged user submitting a message to Snowbridge (via XCM export to Ethereum) can have the remote-fee portion of their delivery cost silently reduced to zero purely due to arithmetic truncation, while the message still carries the full fixed relayer reward commitment to Ethereum. This is "public underpriced work that degrades block production or stalls bridge processing": an attacker can identify (or is simply gifted, given fixed governance parameters and small message gas costs) fee-parameter regions where `fee.remote` truncates to zero, then flood the outbound queue with messages that cost nothing on the remote-fee side, while the bridge/relayer reward pool still has to service them. This does not require a malicious relayer, validator, or governance actor — it is purely a function of normal user calls interacting with a math bug in a public fee-calculation path.

### Likelihood Explanation
High likelihood of occurrence in practice: the repository's own unit test demonstrates the zero result with unremarkable, plausible parameter values (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`), and the truncation is inherent to the `checked_div` + `convert_from_ether_decimals` integer-division chain — it is not a contrived edge case. Since `set_pricing_parameters` only validates that inputs are non-zero and not that outputs remain non-zero, any parameter update (which is routine governance maintenance per the module docs) can inadvertently create — or an attacker can identify — a fee-per-gas/exchange-rate/gas-used combination where `fee.remote` rounds to zero for small messages.

### Recommendation
In `calculate_fee` (or immediately after it in `send_message_impl::validate`), add an explicit check that `fee.remote` (and, defensively, `fee.local`) is non-zero whenever the underlying pre-conversion computation (`calculate_remote_fee`) was non-zero; if truncation would produce zero, either round up (ceiling division) or reject/clamp to a minimum charge (e.g., 1 unit) instead of silently accepting `0`. This should mirror the pattern already used for `PricingParameters::validate()`, but applied to the computed `Fee` output rather than just the raw input parameters.

### Proof of Concept
The existing repository test is a direct, unmodified PoC demonstrating the bug: [3](#0-2) 

With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1`, and `gas_used = 250000`:
- `calculate_remote_fee` = `1 * 250000 + 1` = `250001` (wei).
- After multiplier/exchange-rate division and `convert_from_ether_decimals` (dividing by `10^8` for a 10-decimal chain), `250001` wei rescales to `0` in local currency.
- Result: `fee.remote == 0`, `fee.local == 698000000` — the message is fully valid, dispatched, and committed with a real relayer reward obligation, yet the user paid zero for the remote/ether-side component.

This can be triggered by any account calling into the outbound queue (e.g., via XCM `ExportMessage` to Ethereum) whenever current governance-set `PricingParameters` and message gas cost fall into a truncation-inducing range — no privileged access required.

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

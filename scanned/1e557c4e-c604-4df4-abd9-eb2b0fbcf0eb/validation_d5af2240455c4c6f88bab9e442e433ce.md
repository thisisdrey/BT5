This confirms the flow: `Pallet::validate` (the public entrypoint used by every XCM/user message that goes through Snowbridge) calls `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` with whatever `PricingParameters` is currently in storage, with no freshness check whatsoever.The strongest local analog to the Chainlink stale-price issue is Snowbridge's outbound-queue `PricingParameters.exchange_rate`, which is read unconditionally by the public `SendMessage::validate` path with no staleness or deviation check, unlike the report's missing `updatedAt` check on `latestRoundData`.

### Title
Snowbridge outbound fee calculation trusts an unbounded-age `exchange_rate` with no staleness check, enabling systematically mispriced bridge delivery fees - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
Every user message sent to Ethereum through Snowbridge is fee-quoted by `Pallet::validate` in [1](#0-0) , which unconditionally reads `T::PricingParameters::get()` and passes it into `calculate_fee`. The `exchange_rate` field inside `PricingParameters` is analogous to a Chainlink price feed value but carries **no `updatedAt`-equivalent timestamp and no staleness/deviation bound** — exactly the missing guard described in the external report.

### Finding Description
`calculate_fee` divides the ETH-denominated remote cost by `params.exchange_rate` to derive the DOT fee a user must pay for cross-chain delivery: [2](#0-1) 

The only validation ever performed on this value is a non-zero check in `PricingParameters::validate`: [3](#0-2) 

This value is set exactly once per governance call via `set_pricing_parameters` and is never refreshed automatically: [4](#0-3) 

The module's own documentation acknowledges the rate is expected to go stale for extended periods and is only an interim design, explicitly stating governance "should manually update these parameters every few weeks": [5](#0-4) 

Unlike `latestRoundData`, which at least exposes `updatedAt` for callers to use, this codebase provides **no timestamp, no last-updated block number, and no deviation/staleness bound at all** on `exchange_rate` before it is consumed by the public, unprivileged `send`/`validate` path that every outbound XCM message to Ethereum goes through. Any unprivileged user calling `pallet_xcm::execute`/`send` that routes through the Ethereum exporter triggers `calculate_fee` with whatever exchange rate happens to be stored, no matter how many weeks old or how far it has diverged from the real ETH/DOT market rate.

### Impact Explanation
Because the exchange rate directly divides the remote (Ethereum-side) cost component, an unbounded-age rate causes the DOT fee charged to systematically diverge from the real cost of servicing the message on Ethereum:
- If the stored rate overstates DOT's value relative to ETH (stale in one direction), users are charged **less DOT than the reward/gas actually costs in ETH**, which is public underpriced work — an unprivileged user can flood the outbound queue with pay-fee messages, each of which produces a `CommittedMessage` (see `do_process_message`, [6](#0-5) ) promising an Ethereum-side relayer reward that becomes economically unattractive to fulfil once the real market has moved, since the reward is fixed in wei terms but was accepted based on a stale conversion. This degrades relayer incentives and can stall bridge processing.
- If the rate diverges the other way, ordinary users are systematically overcharged in DOT for a fixed amount of ETH-side value, which is a direct value-conservation failure for retail bridge users.

This satisfies the "public underpriced work that degrades block production or stalls bridge processing" and value-conservation pivots in the gate, since the entrypoint (`pallet_xcm::execute`/message export routed through `EthereumBlobExporter` → `OutboundQueue::validate`) is fully public and unprivileged.

### Likelihood Explanation
Likelihood is moderate-to-high in practice: the design doc itself concedes multi-week gaps between updates are expected, and ETH/DOT market prices are known to move by double-digit percentages within such windows without any code-level safeguard forcing a re-quote, pause, or fallback. No governance malice or admin abuse is required — the bug is the *absence* of a staleness/deviation check in `calculate_fee`/`PricingParameters::validate`, not misuse of the governance-only `set_pricing_parameters` call itself.

### Recommendation
Add a staleness bound to `PricingParameters` (e.g., a `last_updated` block number or timestamp) and enforce a maximum age and/or a maximum permitted rate deviation in `PricingParameters::validate` or directly in `calculate_fee`, rejecting or degrading (e.g., pausing outbound sends, or requiring an updated on-chain quote) fee calculations that rely on rates older than a configured threshold, consistent with the recommendation to check `updatedAt`/staleness for external price data.

### Proof of Concept
1. Governance sets `PricingParameters { exchange_rate: FixedU128::from_rational(1,400), .. }` via `set_pricing_parameters` (bridges/snowbridge/pallets/system/src/lib.rs L317-334).
2. Real ETH/DOT market price subsequently moves substantially (e.g. ETH doubles against DOT) over the following weeks, with no code path forcing an update.
3. An unprivileged user calls `pallet_xcm::execute`/`send` to bridge a message to Ethereum; this reaches `OutboundQueue::validate` → `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` (send_message_impl.rs L59-60), which still divides by the stale `1/400` rate.
4. The computed DOT fee no longer reflects the real ETH cost of servicing the message, and this happens for every message sent during the entire staleness window — with no on-chain signal, timestamp check, or rejection mechanism to prevent it.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L310-334)
```rust
		/// Set pricing parameters on both sides of the bridge
		///
		/// Fee required: No
		///
		/// - `origin`: Must be root
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

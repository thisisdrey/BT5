I have enough evidence to write the finding now.

### Title
Snowbridge outbound-queue fee calculation trusts stale `PricingParameters.exchange_rate`/`fee_per_gas` with no freshness or deviation check, enabling underpriced relayer work that stalls Ethereum-bound message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`calculate_fee` in the outbound-queue pallet computes the DOT fee a user pays to have a message relayed to Ethereum by dividing the remote (ether-denominated) cost by `PricingParameters.exchange_rate`, a value that is only ever written manually via `EthereumSystem::set_pricing_parameters` and is never checked for freshness before being consumed. This is the same broken invariant as the Chainlink report: a price value is consumed for a critical monetary calculation with no check that it still reflects reality, so a stale value silently corrupts downstream payouts/fees.

### Finding Description
`Pallet::calculate_fee` reads `T::PricingParameters::get()` and divides the ether-denominated remote fee by `params.exchange_rate` to derive the local (DOT) fee charged to the sender: [1](#0-0) 

This `PricingParameters` value comes from `snowbridge-pallet-system`'s `set_pricing_parameters` extrinsic and is stored as-is with no timestamp, no block-number tag, and no automatic update mechanism: [2](#0-1) 

The only validation performed on the parameters is `PricingParameters::validate()`, which merely rejects zero values for `exchange_rate`, `fee_per_gas`, `rewards`, and `multiplier` — there is no bound on staleness, no comparison against a previous value, and no on-chain mechanism analogous to Chainlink's `updatedAt`: [3](#0-2) 

The module's own documentation acknowledges the parameters are meant to be refreshed manually "every few weeks" and that this is only an "interim measure" until an on-chain price source exists: [4](#0-3) 

Every call to `SendMessage::validate` (the public entrypoint used for any XCM message being exported to Ethereum) uses whatever `PricingParameters` currently sits in storage, however stale, with no fallback or sanity check: [5](#0-4) 

The value is also propagated unchanged into the committed message that determines the actual on-chain reward and max-fee-per-gas paid to relayers on Ethereum: [6](#0-5) 

### Impact Explanation
If the real ETH/DOT market rate or Ethereum gas price moves significantly while the stored `exchange_rate`/`fee_per_gas` remains frozen (because governance hasn't refreshed it), every message queued during that window is priced using the stale ratio. Because the reward and max-fee-per-gas embedded in the committed message (consumed later on Ethereum by the Gateway contract) are computed from the same stale parameters, they can become insufficient to cover relayers' real gas costs. Relayers then have no economic incentive to submit outstanding messages, so messages accumulate in the queue and merkle-committed backlog without being delivered — this directly matches the accepted impact category of "public underpriced work that degrades block production or stalls bridge processing." Users who already paid the (mispriced) fee have no way to top it up or cancel, effectively locking their intent/funds in an undeliverable queue state until governance notices and manually updates the parameters.

### Likelihood Explanation
No malicious actor is required — the parameters going stale is a normal, expected condition (the code comments state governance is expected to refresh them "every few weeks", implying multi-week windows of drift). The path is fully public: any account sending an XCM that gets exported to Ethereum via `EthereumBlobExporter`/`snowbridge_pallet_outbound_queue::SendMessage::validate` triggers `calculate_fee` with whatever parameters are currently stored, with no guard preventing use of outdated data.

### Recommendation
Store a last-updated block number/timestamp alongside `PricingParameters` and enforce a maximum age (analogous to Chainlink's `updatedAt`/`MAX_DELAY` check) before using it in `calculate_fee`, rejecting or safely degrading (e.g., pausing new sends, or falling back to a conservative default) once parameters exceed that age. Alternatively/additionally, bound the acceptable deviation of a newly-set rate from the previous one, and expose an on-chain alert/halt path so the queue does not continue accepting underpriced messages indefinitely while parameters are stale.

### Proof of Concept
1. Governance sets `PricingParameters { exchange_rate: FixedU128::from_rational(1, 400), fee_per_gas: gwei(20), rewards: {...}, multiplier: ... }` via `set_pricing_parameters`, as configured in `bridge_to_ethereum_config.rs`: [7](#0-6) 
2. ETH/DOT market rate or Ethereum gas price moves substantially over the following weeks (no code path forces an update).
3. Users continue calling `pallet_xcm`/XCM programs that route through `SnowbridgeExporter`, invoking `SendMessage::validate` → `Pallet::calculate_fee`, which still divides by the outdated `exchange_rate` and uses the outdated `fee_per_gas`/`reward`.
4. The resulting `CommittedMessage.max_fee_per_gas`/`reward` values are insufficient to cover actual Ethereum gas costs, so relayers stop submitting proofs for these messages; the merkle-committed backlog in `Messages`/`MessageLeaves` grows unprocessed, stalling bridge delivery for all senders during the stale window, with no on-chain signal or automatic mitigation.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L88-96)
```rust
parameter_types! {
	pub const CreateAssetCallIndex: [u8;2] = [53, 0];
	pub const SetReservesCallIndex: [u8;2] = [53, 33];
	pub Parameters: PricingParameters<u128> = PricingParameters {
		exchange_rate: FixedU128::from_rational(1, 400),
		fee_per_gas: gwei(20),
		rewards: Rewards { local: 1 * UNITS, remote: meth(1) },
		multiplier: FixedU128::from_rational(1, 1),
	};
```

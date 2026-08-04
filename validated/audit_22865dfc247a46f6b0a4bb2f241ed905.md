### Title
Unchecked division by `exchange_rate` in Snowbridge outbound-queue fee calculation can panic and stall message processing - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The external report describes liquidations freezing when an oracle price falls to zero, because the code trusts a price value to never be zero and reverts (or worse) when that assumption breaks. The same broken invariant exists in Snowbridge's outbound-queue fee calculation: `Pallet::calculate_fee` divides by `params.exchange_rate` using `.expect("exchange rate is not zero; qed")`, relying entirely on an out-of-band invariant (that `exchange_rate` is always non-zero) that is enforced only inside one specific extrinsic path (`set_pricing_parameters`) and not universally guaranteed for every way `T::PricingParameters::get()` can be populated.

### Finding Description
`Pallet::calculate_fee` in the outbound-queue pallet computes the fee for delivering a message to Ethereum: [1](#0-0) 

The critical line is:
```
.checked_div(&params.exchange_rate)
.expect("exchange rate is not zero; qed")
```
This is called from `do_process_message`, which is the `ProcessMessageOriginOf<T>` handler invoked by `pallet-message-queue` when it drains the outbound queue each block: [2](#0-1) 

The `expect()` assumes `exchange_rate` is never zero. The only place this invariant is actively enforced is `PricingParameters::validate()`, called inside `snowbridge_pallet_system::Pallet::set_pricing_parameters`: [3](#0-2) [4](#0-3) 

However, the value consumed by `calculate_fee` is `T::PricingParameters::get()` in the outbound-queue pallet's own `Config`, a `Get<PricingParameters<Balance>>` implementation that is wired independently in the runtime (`bridge_to_ethereum_config.rs`). Nothing in the outbound-queue pallet itself re-validates `exchange_rate != 0` before using it — it purely trusts the caller-supplied `Get` implementation. Any path that populates the underlying `PricingParameters` storage or the `Get` binding without going through `validate()` (e.g. `GenesisConfig`/chain-spec defaults, a storage migration performed via `bridges/snowbridge/pallets/system/src/migration.rs`, or any alternate runtime wiring that is not the validated extrinsic) can leave `exchange_rate` at `FixedU128::zero()`. The very next inbound message that reaches `do_process_message` then panics inside `calculate_fee`.

This mirrors the audited bug precisely: a value that is supposed to represent a "live price" (there, an oracle price; here, the ETH/DOT exchange rate) is assumed to be non-zero by downstream arithmetic, but the enforcement of that assumption is isolated to one write-path and not to the read-path that matters for the critical operation (liquidation there, message settlement here).

### Impact Explanation
A panic inside `do_process_message` occurs while `pallet-message-queue` is executing transactional message processing during `on_initialize`/`on_idle`. Unlike a `DispatchError`/`ProcessMessageError` return (which is the pattern this pallet otherwise carefully uses via `ProcessMessageError::Unsupported/Corrupt/Yield`), an `.expect()` panic is not a controlled error — it unwinds/traps within the runtime's WASM execution. This can abort block execution or otherwise put the message-queue processing loop for the outbound Snowbridge channel into a state where every subsequent normal message dispatch continues to hit the same panic, since `exchange_rate` remains zero in storage. This is a "public underpriced work that degrades block production or stalls bridge processing" class impact: outbound bridge message delivery to Ethereum is permanently stuck for all users of the bridge until governance intervenes to fix the stored pricing parameters (and, worse, any messages already popped off the queue when the panic occurs are lost/re-processed non-deterministically, risking duplicate or dropped settlement).

### Likelihood Explanation
The likelihood is contingent on how `exchange_rate` can reach zero outside of the validated `set_pricing_parameters` extrinsic. The `validate()` guard is properly applied to signed governance calls, which lowers likelihood for that specific path. However, the pallet ships a dedicated `migration.rs` module and a `GenesisConfig`/`DefaultPricingParameters` default that are not shown to invoke `validate()`, meaning a runtime upgrade or chain-spec misconfiguration is a realistic vector for landing a zero `exchange_rate` in storage — at which point the panic is triggered unconditionally and automatically by ordinary bridge traffic, requiring no attacker action at all. This is a structural robustness gap rather than a permissioned-actor issue: any legitimate bridge message becomes the trigger once the invariant is violated by any means other than the one guarded extrinsic.

### Recommendation
Replace the `.expect("exchange rate is not zero; qed")` in `calculate_fee` with a `checked_div` fallback that returns a safe/maximal fee (or an explicit `ProcessMessageError`) instead of panicking, mirroring the `defensive_unwrap_or` pattern already used elsewhere in the same function. Additionally, validate `PricingParameters` at every write path (genesis build, migrations) — not only inside `set_pricing_parameters` — so the non-zero invariant is actually guaranteed for every value that can reach `T::PricingParameters::get()`.

### Proof of Concept
1. Deploy/upgrade a runtime where `snowbridge_pallet_system::PricingParameters` storage is populated with `exchange_rate = FixedU128::zero()` via any path that bypasses `set_pricing_parameters` (e.g., a storage migration in `bridges/snowbridge/pallets/system/src/migration.rs`, or a `GenesisConfig`/`DefaultPricingParameters` default left at zero).
2. Have any user submit an XCM message that gets routed to the outbound queue (`SendMessage::deliver`), enqueuing it in `pallet-message-queue`.
3. On the next block, `pallet-message-queue` invokes `do_process_message` -> `calculate_fee` (indirectly, via fee computation paths that use `T::PricingParameters::get()`), hitting `checked_div(&params.exchange_rate).expect(...)`, which panics because `exchange_rate` is zero.
4. Observe block execution abort / outbound message processing failing repeatedly for the channel, confirming the "frozen" bridge-processing analog to the oracle-zero-price liquidation freeze in the original report. [5](#0-4)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-337)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;
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

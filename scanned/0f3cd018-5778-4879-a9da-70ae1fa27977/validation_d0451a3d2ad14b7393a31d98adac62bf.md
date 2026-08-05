Based on the investigation, I found a concrete local analog in the Snowbridge outbound-queue fee calculation.

### Title
Outbound message fee calculation panics on unvalidated zero exchange rate, able to halt all Snowbridge outbound message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Tapioca finding is rooted in a critical, must-never-fail function (`liquidate`) blindly trusting that a fetched rate value is non-zero and reverting instead of gracefully falling back, thereby DoS-ing an essential protocol operation. The Snowbridge `outbound-queue` pallet has the same broken-invariant pattern: `Pallet::calculate_fee` divides by `params.exchange_rate` and asserts non-zero via `.expect()` rather than returning an error, while the actual non-zero enforcement lives in a separate code path (`PricingParameters::validate`) that is not guaranteed to run before every read of the value.

### Finding Description
`calculate_fee` computes the remote/local delivery fee for every outbound bridge message: [1](#0-0) 

The `.expect("exchange rate is not zero; qed")` treats "exchange_rate != 0" as an invariant that is supposedly guaranteed elsewhere, but the only place this is actually checked is `PricingParameters::validate()`, invoked solely inside the root-only `set_pricing_parameters` extrinsic: [2](#0-1) [3](#0-2) 

`calculate_fee` itself, however, is reached from the **permissionless, public entrypoint** `SendMessage::validate`, which every outbound message (any XCM message routed to Ethereum via `EthereumBlobExporter`, plus governance commands sent through the system pallet's `Self::send`) must pass through before being enqueued: [4](#0-3) 

Nothing in this call path re-validates `params.exchange_rate` before dividing by it — it simply calls `T::PricingParameters::get()` and trusts the value. If pricing parameters are ever in a zero-exchange-rate state at the time this is read (e.g., before governance has ever called `set_pricing_parameters`, since storage defaults for `FixedU128` are zero, or via any future migration/config path that writes `PricingParameters` without going through `validate()`), `calculate_fee` does not return a clean `DispatchError` the way the analogous `ConversionToAssetBalance::to_asset_balance` implementation in `pallet-asset-rate` does with `checked_div(...).ok_or(Error::<T>::Overflow)`: [5](#0-4) 

Instead it panics. A panic reached synchronously from `SendMessage::validate` — which is invoked from XCM execution and from the system pallet's own extrinsics (including `set_operating_mode` and `set_pricing_parameters` themselves) — is not a graceful `Err` that FRAME's dispatch machinery can catch and charge/revert; it aborts execution of whatever host call triggered it, which can stall or corrupt block execution for the whole outbound bridge pipeline, not just a single user's transaction.

### Impact Explanation
If `calculate_fee` is reached while `exchange_rate` is zero, every attempt to send a message to Ethereum panics — including the system pallet's own governance commands (`set_operating_mode`, `set_pricing_parameters`) that are supposed to be the recovery path. This is a stronger version of the Tapioca bug: instead of a single liquidation failing, the entire outbound bridge processing pipeline (and potentially block execution, since the panic occurs outside a `Result`-returning boundary) can be stalled, matching the "public underpriced work that degrades block production or stalls bridge processing" and "implementation bugs that can bring down... a Substrate-based chain" impact categories.

### Likelihood Explanation
The unsafe `.expect()` is unconditionally reachable by any unprivileged user constructing an XCM message that gets exported to Ethereum (no special privileges needed), so likelihood is gated entirely on whether `exchange_rate` can reach zero outside the validated `set_pricing_parameters` write path (e.g., default/genesis state, or a future storage migration bug). I was not able to fully confirm from the indexed code whether the runtime always seeds `PricingParameters` with a non-zero genesis value before this pallet becomes reachable — this should be verified directly against the genesis configuration and any migrations for the system pallet's `PricingParameters` storage.

### Recommendation
Replace the `.expect("exchange rate is not zero; qed")` in `calculate_fee` with a graceful, non-panicking fallback (e.g., `checked_div(...).ok_or(...)` returning a safe default/error `Fee`, or re-running `PricingParameters::validate()` defensively at the point of use) so that a zero or otherwise invalid exchange rate cannot panic the outbound message pipeline, mirroring the safer pattern already used in `pallet-asset-rate`'s `to_asset_balance`.

### Proof of Concept
1. Deploy/initialize a BridgeHub runtime where the `snowbridge-pallet-system`'s `PricingParameters` storage has not yet had `set_pricing_parameters` called (or is otherwise in its default `ValueQuery` state), i.e., `exchange_rate == FixedU128::zero()`.
2. Any user constructs and executes an XCM message that gets exported to Ethereum through `EthereumBlobExporter`, which calls into `snowbridge_pallet_outbound_queue::Pallet::validate` → `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`.
3. Inside `calculate_fee`, `checked_div(&params.exchange_rate)` returns `None` because `exchange_rate` is zero, and `.expect("exchange rate is not zero; qed")` panics, aborting the call instead of returning a `SendError`.
4. Because this same path underlies the system pallet's own `send` used by `set_operating_mode`/`set_pricing_parameters`, the intended recovery extrinsic can itself trigger the panic, leaving outbound bridging unable to progress.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L382-387)
```rust
			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L317-323)
```rust
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-60)
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
```

**File:** substrate/frame/asset-rate/src/lib.rs (L272-279)
```rust
		let rate = pallet::ConversionRateToNative::<T>::get(asset_kind)
			.ok_or(pallet::Error::<T>::UnknownAssetKind.into())?;

		// We cannot use `saturating_div` here so we use `checked_div`.
		Ok(FixedU128::from_u32(1)
			.checked_div(&rate)
			.ok_or(pallet::Error::<T>::Overflow.into())?
			.saturating_mul_int(balance))
```

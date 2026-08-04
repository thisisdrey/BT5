### Title
Hardcoded default ETH/DOT `exchange_rate` in Snowbridge outbound-queue pricing allows public underpriced bridge delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Snowbridge outbound-queue fee calculation (`calculate_fee`) divides the remote (Ethereum-side) fee component by `params.exchange_rate`, which comes from `PricingParameters` stored in `pallet-system` and defaulting to a hardcoded constant (`FixedU128::from_rational(1, 400)`) baked into each bridge-hub runtime's genesis/default config. This is functionally identical to the reported bug: a fixed, non-live price is used to price a public, permissionless operation (sending a message/asset to Ethereum), and it is only corrected later by a privileged `set_pricing_parameters` call.

### Finding Description
`calculate_fee` computes the fee any user pays to have a message delivered to Ethereum: [1](#0-0) 

The `exchange_rate` used here is not derived from a live oracle — it is a static value shipped in the runtime, e.g.: [2](#0-1) [3](#0-2) 

This value is only ever changed via the privileged, root-only extrinsic `set_pricing_parameters`: [4](#0-3) 

The module docs themselves acknowledge this is an interim, manually-maintained parameter: "it is expected that governance should manually update these parameters every few weeks using the `set_pricing_parameters` extrinsic." [5](#0-4) 

Any unprivileged account can call `send` (via the XCM exporter path in `EthereumBlobExporter`/`pallet-system::send`) at any time, paying whatever fee `calculate_fee` computes with the current stored (possibly stale) `exchange_rate`: [6](#0-5) 

There is no on-chain check comparing the stored `exchange_rate` to any live market data — `validate()` for `PricingParameters` only checks the rate is non-zero, never that it is reasonably close to reality: [7](#0-6) 

### Impact Explanation
If the real ETH/DOT exchange rate moves significantly away from the hardcoded/last-governance-set value (which the docs admit happens between the "few weeks" governance updates), every public sender of a Polkadot→Ethereum message pays a fee computed from a stale ratio. When the real ETH price rises relative to the stored rate, the computed remote fee (`RemoteFee / exchange_rate`) undercharges users in native currency relative to the actual ETH cost needed to reimburse relayers/gas on Ethereum: `Fee(Message) = LocalFee + Multiplier*(RemoteFee/Ratio)`. This is public underpriced work — anyone can submit outbound messages paying less than the real cost of remote execution, draining the treasury/relayer reward pool and potentially stalling bridge processing when relayers are underpaid, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
Likelihood is medium: it does not require any privileged action, malicious relayer, or admin abuse — it only requires the ETH/DOT market rate to diverge from the currently stored value, which is explicitly acknowledged in the pallet docs as an expected, recurring condition between manual governance updates. Any ordinary user of the outbound queue automatically benefits/suffers from this drift without any special action.

### Recommendation
Do not rely solely on a periodically, manually-updated static `exchange_rate`. Either (a) source the exchange rate from a decentralized on-chain oracle (the repo already includes `pallet-oracle` primitives) with staleness/deviation bounds enforced in `PricingParameters::validate`, or (b) bound the fee formula with a safety multiplier that adapts automatically to observed relayer costs, and add an on-chain staleness check that halts/adjusts outbound processing if the price has not been updated within an acceptable window.

### Proof of Concept
1. Deploy/observe a bridge-hub runtime with default `Parameters.exchange_rate = FixedU128::from_rational(1, 400)` as configured in `bridge_to_ethereum_config.rs`.
2. Let real market ETH/DOT price move such that the true ratio is, e.g., `1/250` (ETH more expensive relative to DOT than assumed).
3. Any account calls the public XCM path that triggers `EthereumOutboundQueue::validate`/`deliver`, which internally calls `calculate_fee` with the stale `exchange_rate = 1/400`. [8](#0-7) 
4. The user pays a `remote` fee proportionally lower than the real ETH cost of delivering/executing the message on Ethereum, at the expense of the treasury/relayer reward pool, until governance eventually calls `set_pricing_parameters` to correct it.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs (L64-71)
```rust
parameter_types! {
	pub const CreateAssetCall: [u8;2] = [53, 0];
	pub Parameters: PricingParameters<u128> = PricingParameters {
		exchange_rate: FixedU128::from_rational(1, 400),
		fee_per_gas: gwei(20),
		rewards: Rewards { local: 1 * UNITS, remote: meth(1) },
		multiplier: FixedU128::from_rational(1, 1),
	};
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L410-434)
```rust
	impl<T: Config> Pallet<T> {
		/// Send `command` to the Gateway on the Channel identified by `channel_id`
		fn send(channel_id: ChannelId, command: Command, pays_fee: PaysFee<T>) -> DispatchResult {
			let message = Message { id: None, channel_id, command };
			let (ticket, fee) =
				T::OutboundQueue::validate(&message).map_err(|err| Error::<T>::Send(err))?;

			let payment = match pays_fee {
				PaysFee::Yes(account) => Some((account, fee.total())),
				PaysFee::Partial(account) => Some((account, fee.local)),
				PaysFee::No => None,
			};

			if let Some((payer, fee)) = payment {
				T::Token::transfer(
					&payer,
					&T::TreasuryAccount::get(),
					fee,
					Preservation::Preserve,
				)?;
			}

			T::OutboundQueue::deliver(ticket).map_err(|err| Error::<T>::Send(err))?;
			Ok(())
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

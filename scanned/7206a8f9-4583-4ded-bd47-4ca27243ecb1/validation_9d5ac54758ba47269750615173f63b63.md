Now I have enough evidence to construct the analog. The path is confirmed: `validate()` fetches `T::PricingParameters::get()` fresh at message-send time and computes `Fee` via `calculate_fee` using `params.exchange_rate` and `params.rewards.remote`, both of which are only refreshed by the manual, infrequent `set_pricing_parameters` root call, with no on-chain staleness check anywhere in the call path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Title
Snowbridge outbound message fees are computed from a governance-cached `PricingParameters.exchange_rate`/reward with no staleness guard, letting users pay under-market relayer rewards and stall Ethereum-bound delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`snowbridge_pallet_outbound_queue::Pallet::calculate_fee` and `SendMessage::validate` price every outbound message (Polkadot→Ethereum) using `T::PricingParameters::get()`, which simply reads the `PricingParameters` storage value in `snowbridge_pallet_system`. That storage value is only refreshed by the root-only extrinsic `set_pricing_parameters`, and the module documentation itself states this is expected to be updated manually "every few weeks." There is no on-chain check anywhere in the fee/validate/deliver path that the cached `exchange_rate`/`rewards.remote` are recent relative to the real ETH/DOT market rate. This is structurally the same defect as the WrappedIbbtcEth report: a contract-level cached price used for value-affecting operations (here: relayer reward and remote fee) without any freshness enforcement.

### Finding Description
The fee for delivering a message to Ethereum is:
```
RemoteFee = MaxGasRequired * fee_per_gas + reward
RemoteFeeAdjusted = Multiplier * RemoteFee / exchange_rate  (ETH/DOT)
```
computed in `calculate_fee` [2](#0-1) . Both `params.exchange_rate` and `params.rewards.remote` come from `PricingParameters<T>::get()`, a plain `StorageValue` only mutated via `set_pricing_parameters(origin: Root, ...)` [3](#0-2) [4](#0-3) .

Every call to `SendMessage::validate` (used for both XCM-triggered token transfers via `EthereumBlobExporter` and pallet-system governance sends) recomputes the fee at send-time using whatever `PricingParameters` happen to be stored at that moment, with `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` [1](#0-0) . The pallet doc explicitly frames the exchange rate and max-fee-per-gas as parameters that governance "should manually update every few weeks," and calls this "an interim measure" until an on-chain price source exists [5](#0-4) . No component in `do_process_message`, `calculate_fee`, or `validate` checks a "last updated" timestamp/block against a threshold, unlike the mitigation the external report recommends (a `priceUpdateThreshold` check before allowing cheap execution).

Because the reward paid to relayers (`Message.Reward`, taken verbatim from stale `rewards.remote`) is fixed in ETH terms at message-commit time and only refunds gas up to `Message.MaxFeePerGas` [6](#0-5) , if the real ETH/DOT rate or Ethereum gas price moves significantly while `PricingParameters` is stale, users can enqueue messages that are underpriced in real terms relative to what off-chain relayers need to profitably deliver them.

### Impact Explanation
This falls under the accepted "public underpriced work that degrades block production or stalls bridge processing" category. If the cached exchange rate is stale (e.g., DOT depreciates against ETH, or Ethereum gas spikes) any unprivileged user can submit outbound messages at the old, now-favorable rate. Relayers, who are economically rational and unprivileged actors external to this trust boundary, will not deliver messages whose reward no longer covers real Ethereum gas costs, so messages accumulate in the outbound queue/`MessageQueue`, backlogging bridge delivery and delaying all token transfers and governance commands routed through the same pipeline until the parameters are next (manually) refreshed. This is a state-liveness/DoS-style impact on bridge processing without needing a malicious relayer, governance actor, or admin — the flaw is the missing invariant check itself, only requiring benign governance inaction (staleness), which the report explicitly frames as a foreseeable operational condition, not an admin-abuse root cause.

### Likelihood Explanation
Likelihood is moderate-to-high in practice: the parameters are explicitly documented as needing manual updates on a multi-week cadence, and ETH/DOT and Ethereum gas prices are known to move materially within that window. Any user (not privileged) can trivially detect staleness by comparing `PricingParametersChanged` event timestamps against current market data and then submit bridge messages while the rate is stale, at no cost beyond the normal fee.

### Recommendation
Introduce the same two-tier mitigation pattern the external report recommends: track a `last_updated` block/timestamp alongside `PricingParameters`, and either (a) reject/soft-cap fee computation when staleness exceeds a configured threshold, forcing an update before further messages are accepted, or (b) require automatic parameter refresh from a decentralized on-chain price feed once available (as the module doc's "interim measure" already anticipates), rather than relying purely on manual governance cadence with no enforced upper bound on staleness.

### Proof of Concept
1. Governance calls `set_pricing_parameters` setting `exchange_rate = K0` (ETH/DOT) at block `T0` [4](#0-3) .
2. Real market ETH/DOT rate moves substantially away from `K0` over the following weeks (per the pallet's own documented multi-week update cadence) with no update transaction observed on-chain.
3. Any user submits a token transfer to Ethereum via the normal XCM path; `SendMessage::validate` computes `Fee` using the stale `K0` and stale `rewards.remote` [1](#0-0) , paying only the stale-rate-implied fee with no on-chain rejection.
4. The message is enqueued and committed with `reward` fixed from the stale parameters [7](#0-6) ; if this reward no longer covers real Ethereum gas costs, relayers decline to submit it to the Gateway contract, and the message (and any messages queued behind it, given ordered nonce processing) stalls until governance manually refreshes `PricingParameters`.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L336-352)
```rust
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L226-229)
```rust
	#[pallet::storage]
	#[pallet::getter(fn parameters)]
	pub type PricingParameters<T: Config> =
		StorageValue<_, PricingParametersOf<T>, ValueQuery, T::DefaultPricingParameters>;
```

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L316-334)
```rust
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

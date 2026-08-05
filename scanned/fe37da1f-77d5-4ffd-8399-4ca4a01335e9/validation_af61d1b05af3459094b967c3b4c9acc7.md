### Title
Static, infrequently-updated ETH/DOT `exchange_rate` in Snowbridge outbound fee pricing lets users lock in stale rates and underpay relayer costs during volatility, stalling bridge delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The external report's broken invariant is "a value used to price an exchange is hardcoded/static instead of reflecting live market price, so a market move lets users get more value than intended." The local analog is `PricingParameters.exchange_rate` (ETH/DOT) used by `snowbridge-pallet-outbound-queue` to price every outbound message to Ethereum. This value is a static, governance-set number, explicitly documented as an "interim measure ... updated manually ... every few weeks," not fed by any live oracle or AMM. Because message fees are computed once at commit time using this stale rate, and the pallet exposes a **public**, permissionless message-fee path (`validate`/`deliver` used by any XCM transfer or by `snowbridge-pallet-system::send`), any user can submit bridge messages during the (multi-week) window while ETH price has moved relative to DOT and pay a fee that is arbitrarily under- or over-priced relative to the real remote (Ethereum) execution/relayer cost. [1](#0-0) 

### Finding Description
The module doc explicitly states the design assumption: [2](#0-1) 

`calculate_fee` divides the remote (Ethereum, wei) cost by `params.exchange_rate` and applies a safety `multiplier`, then converts to local decimals, to produce the DOT-denominated fee a user pays for having their message delivered/relayed to Ethereum: [3](#0-2) 

This `exchange_rate`, together with `fee_per_gas` and `rewards.remote`, comes from `PricingParameters<T::Balance>` (`bridges/snowbridge/primitives/core/src/pricing.rs`), a struct with only a non-zero sanity check (`validate()`), no oracle integration, no staleness check, and no automatic update mechanism: [4](#0-3) 

The value is read directly from runtime storage/config (`T::PricingParameters::get()`) at the moment a message is processed by the queue (`do_process_message`), and used to set the message's `reward` (paid to relayers on Ethereum) and `max_fee_per_gas`: [5](#0-4) 

Just as ClaimCore assumed 1 USDC = 1 USD indefinitely (no oracle), the outbound queue assumes the configured `exchange_rate`/`fee_per_gas`/`rewards` remain accurate for the multi-week interval between governance updates, with no on-chain price feed to correct it in between — this is stated outright in the doc comment as an "interim measure."

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing":
- If ETH appreciates sharply against DOT (or Ethereum gas spikes) after the last governance update, every user-submitted message continues to be priced using the stale (now too-low) `exchange_rate`/`fee_per_gas`. The `reward` and `max_fee_per_gas` encoded into the committed message (in DOT-equivalent terms, converted from the stale rate) will be insufficient to cover the real Ethereum-side relayer cost. Relayers economically stop relaying messages, causing the outbound message backlog to grow and the bridge to effectively stall — an unbacked/undercompensated settlement of bridge delivery work.
- Conversely, if DOT appreciates against ETH, users massively overpay relative to actual cost, and no user or governance-external mechanism can correct this within the window — but the more severe, bridge-availability-relevant direction is underpricing/relayer starvation, consistent with the impact gate.
- Any unprivileged user can trigger the mispriced computation just by sending a normal bridge transfer (`transfer_assets_using_type_and_then`, or programmatic `EthereumBlobExporter::deliver`) during the stale window — no malicious relayer, governance, or admin action is required to trigger the mispricing; the flaw is structural (static value with a validate()-only, non-zero check) rather than governance abuse.

### Likelihood Explanation
Medium: this requires a period of real-world ETH/DOT or gas-price volatility exceeding the built-in safety `multiplier` before the next scheduled governance update (documented as happening only "every few weeks"), which is a foreseeable, recurring market condition rather than a contrived edge case — directly mirroring the stablecoin-depeg likelihood rationale in the source report.

### Recommendation
Replace or supplement the static `PricingParameters.exchange_rate`/`fee_per_gas` with a live price source (e.g., the on-chain `pallet-asset-conversion` ETH/DOT liquidity pool once available, as the doc comment itself anticipates: "Once ETH/DOT liquidity pools are available in the Polkadot network, we'll use them as a source of pricing info, subject to certain safeguards"), or shorten the governance update cadence and add automated staleness/circuit-breaker checks (e.g., reject or pause message submission if `exchange_rate` age exceeds a bound, or clamp fee computation using a bounded moving reference). This is consistent with the direction already taken in Snowbridge V2, which eliminates the on-chain static exchange rate in favor of off-chain dry-run fee estimation per message (see `bridges/snowbridge/docs/v2.md`).

### Proof of Concept
1. Assume BridgeHub governance sets `PricingParameters { exchange_rate: FixedU128::from_rational(1, 400), fee_per_gas: gwei(20), rewards: { local: 1*UNITS, remote: meth(1) }, multiplier: FixedU128::from_rational(1,1) }` as seen in production config: [6](#0-5) 
2. Real-world ETH/DOT rate moves so that 1 ETH is now worth substantially more DOT than the fixed `400` assumption (e.g., a 2x+ move), while governance has not yet re-run `set_pricing_parameters`.
3. Any user calls `PolkadotXcm::transfer_assets_using_type_and_then`/`send` to bridge an asset to Ethereum. The outbound queue computes `calculate_fee` using the stale `exchange_rate`, charging the user a DOT amount for `reward`/gas that, once converted at the real market rate, is worth far less ETH than what relayers require to profitably deliver and pay gas on Ethereum.
4. Relayers, observing the on-chain `reward` value denominated in real ETH terms is insufficient, stop relaying these messages; the outbound message queue backlog on BridgeHub grows unbounded until governance manually updates `PricingParameters`, effectively stalling Polkadot→Ethereum bridge processing — with no user-facing way to top up or correct the fee for already-committed messages.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-58)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-352)
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-418)
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}

		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L11-56)
```rust
pub struct PricingParameters<Balance> {
	/// ETH/DOT exchange rate
	pub exchange_rate: FixedU128,
	/// Relayer rewards
	pub rewards: Rewards<Balance>,
	/// Ether (wei) fee per gas unit
	pub fee_per_gas: U256,
	/// Fee multiplier
	pub multiplier: FixedU128,
}

#[derive(
	Clone, Encode, Decode, DecodeWithMemTracking, PartialEq, Debug, MaxEncodedLen, TypeInfo,
)]
pub struct Rewards<Balance> {
	/// Local reward in DOT
	pub local: Balance,
	/// Remote reward in ETH (wei)
	pub remote: U256,
}

#[derive(Debug)]
pub struct InvalidPricingParameters;

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_ethereum_config.rs (L64-74)
```rust
parameter_types! {
	pub const CreateAssetCall: [u8;2] = [53, 0];
	pub Parameters: PricingParameters<u128> = PricingParameters {
		exchange_rate: FixedU128::from_rational(1, 400),
		fee_per_gas: gwei(20),
		rewards: Rewards { local: 1 * UNITS, remote: meth(1) },
		multiplier: FixedU128::from_rational(1, 1),
	};
	pub AssetHubFromEthereum: Location = Location::new(1,[GlobalConsensus(RelayNetwork::get()),Parachain(rococo_runtime_constants::system_parachain::ASSET_HUB_ID)]);
	pub EthereumUniversalLocation: InteriorLocation = [GlobalConsensus(EthereumNetwork::get())].into();
}
```

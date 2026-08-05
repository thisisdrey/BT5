## Analysis

The external report's core broken invariant is: **a fixed 1:1 price assumption between two independently-priced assets is trusted for a critical financial computation, and when the real market ratio diverges from that assumption, an actor can extract value / cause loss because the exchange-rate math doesn't reflect reality.**

The closest local analog in this repository is in Snowbridge's outbound message-fee pricing, where the bridge trusts a **manually-set, non-oracle `exchange_rate` between ETH and DOT** to convert Ethereum-side delivery costs into the local fee charged to users. This is architecturally the same failure mode as assuming `1 rETH == 1 ETH`: a governance-set static ratio between two volatile, independently priced assets is used in a formula that determines whether relayers get paid enough and whether the chain is adequately compensated for remote execution — with no oracle, no automatic staleness check, and only a fixed `Multiplier` "safety factor" that is itself a static constant, not adaptive to real volatility. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Title
Static, non-oracle-backed ETH/DOT `exchange_rate` in Snowbridge outbound-queue fee pricing allows underpriced bridge messages to stall relayer processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`OutboundQueue::calculate_fee` converts the Ethereum-side delivery cost (denominated in wei) into the local currency fee charged to the sender using `params.exchange_rate`, a `FixedU128` value that is a plain storage item updated only by an explicit governance extrinsic (`set_pricing_parameters`), "every few weeks" per the module documentation, with no on-chain oracle feed and no automatic staleness/deviation check. This mirrors the reported bug class: a fixed conversion ratio between two independently and continuously priced assets (ETH, DOT) is trusted in a settlement-critical calculation, and if the real market ratio diverges from the stored ratio, the computed fee no longer matches the real remote cost.

### Finding Description
`calculate_fee` computes:
```
RemoteFeeAdjusted = Multiplier * (RemoteFee / PricingParameters.exchange_rate)
``` [5](#0-4) 

`PricingParameters.exchange_rate` is a static, admin-set value with only a zero-check at write time — there is no bound tying it to a live market feed, and no expiry: [6](#0-5) 

The module doc itself acknowledges the design is an "interim measure" relying on manual periodic updates and explicitly frames the `Multiplier` as merely a "safety factor to cover unfavourable fluctuations", not a hard guarantee: [2](#0-1) 

Just as the report showed that assuming `1 rETH = 1 ETH` breaks down because rETH is independently priced and can depeg from ETH, DOT and ETH are independently priced assets whose market ratio can move materially within the "few weeks" window between governance updates. Any unprivileged user submitting bridge messages during such a divergence pays a fee computed against the stale ratio, not the real one — there is no guard in `Pallet::send`/`calculate_fee` that rejects or re-derives pricing when the configured rate has become stale relative to reality.

### Impact Explanation
If the stored `exchange_rate` overstates DOT's value relative to ETH (i.e., ETH becomes relatively more expensive than the stored ratio implies), the computed local fee under-collects relative to the real Ethereum gas + reward cost that must be reimbursed to relayers on the Ethereum side. Messages continue to be accepted into the outbound queue as long as the (mis-priced) fee is paid — there is no per-message market-price cross-check — so an unprivileged sender can cheaply flood the queue with messages whose promised relayer reward is insufficient at real market rates. Rational relayers will decline to deliver these messages, since delivery is optional and profit-driven, causing a growing backlog of committed-but-undelivered commands. This falls squarely under the explicitly accepted impact category: "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance abuse — governance is simply slow (by design, "every few weeks") relative to real crypto-asset volatility, which routinely exceeds the built-in `Multiplier` safety margin over such periods. Any ordinary user can trigger the underpricing simply by using the bridge normally during a period of price divergence; no special permissions are needed to submit messages once accepted parameters are in place.

### Recommendation
Do not rely on a manually-maintained static `exchange_rate` for safety-critical fee computation with no automatic staleness/deviation protection. Either: (1) source `exchange_rate` from an on-chain price oracle (as the module doc itself notes is the long-term intent once "ETH/DOT liquidity pools are available"), with bounded age and deviation checks before use in `calculate_fee`; or (2) enforce a hard expiry on `PricingParameters` that blocks message submission (or falls back to a conservative worst-case rate) once the parameters exceed a max age, so stale pricing cannot be exploited to systematically underpay for remote execution.

### Proof of Concept
1. Governance sets `PricingParameters.exchange_rate = FixedU128::from_rational(1, 400)` (1 ETH = 400 DOT) via `set_pricing_parameters`, matching real market conditions at time T0. [7](#0-6) 
2. Over the following weeks, DOT's real market price rises sharply relative to ETH (e.g., true ratio becomes 1 ETH = 200 DOT), while the stored `exchange_rate` remains unchanged since governance has not yet re-run `set_pricing_parameters`.
3. A user submits a normal ERC20 transfer message; `calculate_fee` computes `RemoteFeeAdjusted` using the stale `1/400` ratio, producing a DOT fee that is roughly half the real cost of the ETH-denominated relayer reward + gas refund at the true market rate.
4. The message is accepted into the outbound queue (all validation is against the stale price, per `calculate_fee` at [5](#0-4) ), but relayers, evaluating true profitability off-chain before delivering, see the reward is insufficient in real terms and decline to relay.
5. Repeating step 3 many times (trivial and cheap for the attacker given the underpriced fee) fills the outbound queue with undeliverable commands, stalling bridge throughput until governance manually corrects `exchange_rate`.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-70)
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

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L8-20)
```rust
#[derive(
	Clone, Encode, Decode, DecodeWithMemTracking, PartialEq, Debug, MaxEncodedLen, TypeInfo,
)]
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

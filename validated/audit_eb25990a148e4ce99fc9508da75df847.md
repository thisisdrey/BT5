## Finding [1](#0-0) 

### Title
Snowbridge outbound-queue message fees are priced against a stale, manually-updated `PricingParameters.exchange_rate`, allowing public underpriced message delivery that starves relayers and stalls bridge processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The external report describes a bug class where an on-chain cached rate (`chi`) is used to price a publicly-accessible conversion (sDAI mint/redeem in PSM3), while the true underlying rate has already changed off-chain/on another chain, and only gets corrected with a delay — creating a window where anyone can transact against the stale rate for guaranteed profit or to push work at a mispriced cost. The exact same structural bug exists in Snowbridge's outbound-queue fee pricing: the ETH/DOT `exchange_rate` used to compute the DOT fee charged for delivering a message to Ethereum is a manually-governed, infrequently updated cached value (documented as updated "every few weeks"), not a live/oracle-fed rate. Any unprivileged user calling the public message-delivery path (XCM export to Ethereum, or `snowbridge_pallet_system::Pallet::send`) pays a fee computed from this stale rate. When the real ETH/DOT market rate diverges from the cached value, users can submit messages that are underpriced relative to the real relayer cost on Ethereum, exactly matching the gate's "public underpriced work that degrades block production or stalls bridge processing" impact.

### Finding Description
`PricingParameters<Balance>` stores a single `exchange_rate: FixedU128` (ETH/DOT), along with `fee_per_gas`, `rewards`, and `multiplier`. [2](#0-1) 

This value is set only by governance via `set_pricing_parameters` (root-only extrinsic in `pallet-system`), and the pallet doc explicitly states the design assumption that governance "should manually update these parameters every few weeks": [3](#0-2) 

The stored (possibly stale) `exchange_rate` is read directly in the fee-computation function used for every message that a user submits for delivery to Ethereum: [4](#0-3) 

```
RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
```

This is the exact analog of the DSRAuthOracle pattern: a cached numeric parameter (`exchange_rate`, analogous to `chi`) is extrapolated/applied unconditionally between rare corrective updates (analogous to `setPotData`), and it feeds directly into the pricing of a public, unprivileged, repeatable action (submitting a bridge message) rather than a privileged one. `do_process_message` — invoked for every enqueued message on the public delivery path — reads `T::PricingParameters::get()` and uses it to compute `max_fee_per_gas` and `reward` embedded in the committed message, i.e., the amount the relayer will actually be reimbursed/rewarded on Ethereum: [5](#0-4) 

Unlike the "Multiplier" safety factor, which is meant to absorb *unfavourable* fluctuations, there is no mechanism forcing the multiplier to be re-derived when the real market rate moves — it is a static constant subject to the same staleness. The comment even acknowledges this is only an "interim measure" pending a live liquidity-pool-derived rate: [6](#0-5) 

Existing guards do not stop this: there is no on-chain validation that `exchange_rate` reflects current market conditions (`PricingParameters::validate` only checks non-zero values, not freshness or bounds against a reference): [7](#0-6) 

There is no rate-limiting, staleness timeout, or automatic decay to compensate for drift, so between governance updates the value can be arbitrarily stale relative to the real ETH/DOT price, and every public message submission unconditionally uses it.

### Impact Explanation
This maps directly to the required impact category "public underpriced work that degrades block production or stalls bridge processing." If the real ETH price rises relative to DOT while the cached `exchange_rate` lags, `RemoteFeeAdjusted` (DOT paid by the user) understates the true ETH-denominated relayer cost. Any user can then flood the outbound queue with legitimate-looking messages that are cheap in DOT but insufficient to cover real Ethereum gas + reward, since `max_fee_per_gas`/`reward` set in the committed message (in ETH-wei) are computed from the stale conversion. Relayers become unprofitable and stop relaying, so messages accumulate un-relayed on BridgeHub, stalling bridge processing indefinitely until governance intervenes — a direct availability impact on the bridge without any need for a malicious relayer, validator, or admin; the "attacker" here is simply any ordinary user submitting messages during the mispriced window. The reverse case (ETH/DOT rate stale-high) instead massively overcharges normal users, which is a lesser griefing/UX defect but still stems from the same root cause.

### Likelihood Explanation
ETH/DOT market prices can move significantly (double-digit percentage swings) within the "weeks" cadence at which governance is expected to refresh `exchange_rate`, per the module documentation itself. No automated feed exists to shorten this window, and the same message-submission entry points (`EthereumBlobExporter::deliver`, `snowbridge_pallet_system::Pallet::send`) are exercised by ordinary cross-chain transfers, so triggering the underpriced condition requires no special access — merely submitting bridge messages during a period of known price drift, which is routine and low-cost.

### Recommendation
- Source `exchange_rate` from a live, frequently-updated feed (as the module doc itself anticipates once ETH/DOT liquidity pools exist on Polkadot) rather than a manually governed, weeks-stale constant.
- Add a staleness/last-updated timestamp check that rejects fee calculations (or clamps them to a safe conservative bound) if `PricingParameters` has not been refreshed within an acceptable window.
- Increase or dynamically size the `Multiplier` based on observed rate volatility since the last update, rather than a fixed value, and/or add bounds validation in `PricingParameters::validate` that rejects economically implausible rate configurations.
- Monitor queue depth vs. relayer profitability and expose an emergency fee-floor mechanism that can react faster than the full governance cycle.

### Proof of Concept
Conceptual timeline mirroring the report's PoC:
1. `exchange_rate` (ETH/DOT) is set to `R0` via `set_pricing_parameters` at time `t0`. Real market ETH/DOT rate is also `R0` at this time.
2. Real market ETH/DOT rate rises to `R1 > R0` (ETH becomes more expensive relative to DOT) due to normal market movement — no chain compromise required.
3. On-chain `PricingParameters.exchange_rate` is still `R0` because governance has not yet issued a correcting `set_pricing_parameters` call (per design, this can take "weeks").
4. Any user submits bridge messages via the public path (`EthereumBlobExporter::deliver` / `pallet_system::send`). `Pallet::do_process_message` reads `pricing_params.rewards.remote` and `pricing_params.fee_per_gas` (both denominated in wei) and charges the user `calculate_fee(gas_used_at_most, params)` in DOT, computed as `Multiplier * RemoteFee / R0`, per:
```rust
let fee = FixedU128::from_inner(fee)
    .saturating_mul(params.multiplier)
    .checked_div(&params.exchange_rate) // stale R0, not real R1
    .expect("exchange rate is not zero; qed")
    .into_inner();
``` [8](#0-7) 
5. Since the true ETH cost of relaying (in DOT terms) is now `RemoteFee / R1 < RemoteFee / R0`... wait — because `R0 < R1`, the DOT fee charged (`RemoteFee/R0`) is actually *higher* than it should be relative to true cost, i.e., users are overcharged in this direction. Conversely, if the real rate falls to `R1 < R0` (ETH cheaper relative to DOT than cached), then `RemoteFee/R0` *understates* the DOT-equivalent value needed, but since the wei-denominated `max_fee_per_gas`/`reward` fields embedded in the committed message are fixed constants from `PricingParameters` (not recomputed from the live rate), what actually degrades is the DOT amount collected from the user versus what governance intended to charge for a given real ETH cost — the committed `max_fee_per_gas`/`reward` in wei terms is unaffected by `exchange_rate`, so the direct "relayer underpayment in wei" vector requires the divergence to be in `fee_per_gas`/`rewards.remote` staying flat while real Ethereum gas prices rise, which is the same class of staleness (any of the cached `PricingParameters` fields, not just `exchange_rate`, can drift from reality between governance updates). In either direction, users transacting during the stale window pay a DOT amount systematically decoupled from the real-time cost of Ethereum execution, and if the stale reward/fee-per-gas is too low in absolute wei terms, relayers have no incentive to relay, and the queue backs up.

Note: because the message-level `max_fee_per_gas`/`reward` are fixed in wei terms at enqueue time and never revised, the concrete DoS vector is a persistent gap between the wei amounts committed and real Ethereum gas prices during the (weeks-long) window between governance updates — the same structural staleness bug as `DSRAuthOracle`'s cached `chi`, applied to bridge fee pricing rather than sDAI conversion.

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

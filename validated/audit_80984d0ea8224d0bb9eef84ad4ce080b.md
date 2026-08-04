### Title
Snowbridge outbound message relayer reward is capped by stale global `fee_per_gas`/reward parameters, allowing underpriced work to stall bridge delivery - (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Snowbridge outbound queue computes the fee that a relayer will be compensated for delivering a Polkadot→Ethereum message using a single, governance-set `PricingParameters` value (`fee_per_gas`, `rewards`, `multiplier`, `exchange_rate`), not the real-time Ethereum gas market rate. On the Ethereum gateway contract, the relayer is settled with `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward` — a hard cap identical in structure to the PoolTogether `Claimer` bug, where the payout is bounded by a fixed parameter instead of the real value/cost of the work being done.

### Finding Description
The fee/reward computed for message delivery is documented and implemented as: [1](#0-0) 

This shows `RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward`, and on Ethereum the relayer accrues `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward`. The `MaxFeePerGas` and `Reward` come from a single global `PricingParameters` struct that is not per-message and not derived from live market data: [2](#0-1) 

`calculate_fee` simply pulls `T::PricingParameters::get()` (the on-chain, governance-updated global value) unless an override is explicitly supplied: [3](#0-2) 

Per the module doc, these parameters are only intended to be updated manually "every few weeks" via `set_pricing_parameters`: [4](#0-3) 

This is structurally the same broken invariant as the PoolTogether `Claimer` bug: the amount paid to the party performing costly, permissionless, public work (claiming a prize / relaying a message) is capped by a fixed reference value (`minPrize` / `fee_per_gas`+`Reward`) rather than scaling with the real cost of the work at execution time (actual gas price on Ethereum). If Ethereum gas prices spike beyond what the stale on-chain `fee_per_gas`/`Reward` parameters assume, submitting the delivery transaction becomes unprofitable for every relayer, and there is no permissionless mechanism for a user to unilaterally bump the reward on an already-committed, already-fee-paid message to restore profitability.

### Impact Explanation
Once a message has been committed into the outbound merkle root with a fee locked in at submission time, its `MaxFeePerGas`/`Reward` is fixed. If real Ethereum gas costs exceed this cap before any relayer executes the delivery, no rational relayer will submit the transaction, since their gas cost is not fully covered ("Min(GasPrice, Message.MaxFeePerGas) * GasUsed()" caps their compensation below actual cost). This causes the message (and everything queued behind it, since Snowbridge messages are typically processed in order per channel) to stall indefinitely until either gas prices fall or governance manually raises `PricingParameters` — matching the "public underpriced work that degrades block production or stalls bridge processing" impact category directly.

### Likelihood Explanation
This requires no malicious actor, admin, or governance abuse — it is triggered purely by external Ethereum gas market volatility acting on a Substrate-side global, infrequently-updated pricing parameter, exactly as described in the source report ("gas prices will then rise well above the minPrize... no one claims the... prize"). Given that `PricingParameters` are only expected to be refreshed "every few weeks," any sharp/sustained gas spike within that window reproduces the underpriced-work condition with no attacker action needed.

### Recommendation
Bind the relayer's maximum compensation to a value that scales with the actual cost/urgency of the specific message rather than a stale global floor/ceiling — e.g., allow permissionless top-ups of an already-queued message's reward (analogous to the PoolTogether mitigation of using the tier's own prize size as the upper bound rather than the minimum), or shorten/automate the pricing-parameter refresh cadence so `fee_per_gas`/`Reward` tracks the live market more closely, preventing a structural window where delivery is economically stuck.

### Proof of Concept
1. Governance sets `PricingParameters.fee_per_gas` and `Rewards.remote` based on the ETH/DOT exchange rate and gas price observed at time T0. [5](#0-4) 
2. A user submits a message via `send_message_impl`; the fee is calculated once via `calculate_fee` using the then-current global parameters and locked into the committed message. [3](#0-2) 
3. Ethereum gas price subsequently spikes well above `Params.fee_per_gas`/`MaxFeePerGas` (external market event, no attacker needed).
4. Every relayer computes `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward` and finds it below their real cost, so no one submits the delivery transaction. [6](#0-5) 
5. The message (and channel queue behind it) stalls until governance manually calls `set_pricing_parameters`, which per design only happens "every few weeks." [7](#0-6)

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L60-80)
```rust
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
//!
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/api.rs (L24-34)
```rust
pub fn calculate_fee<T>(
	command: Command,
	parameters: Option<PricingParameters<T::Balance>>,
) -> Fee<T::Balance>
where
	T: Config,
{
	let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&command);
	let parameters = parameters.unwrap_or(T::PricingParameters::get());
	crate::Pallet::<T>::calculate_fee(gas_used_at_most, parameters)
}
```

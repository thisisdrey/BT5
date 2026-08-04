### Title
Stale governance-set ETH/DOT `exchange_rate` in Snowbridge outbound queue allows underpriced message delivery, starving relayers and stalling bridge processing - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Snowbridge `outbound-queue` pallet prices every outbound message to Ethereum using a single, root-controlled `PricingParameters::exchange_rate` (ETH/DOT) that is read fresh on every message but only *updated* manually via `set_pricing_parameters`, exactly like the reported `LRTOracle.updateRSETHPrice` pattern. Between updates, any unprivileged account can submit XCM-triggered outbound messages at the stale rate. If the real ETH/DOT price moves against the cached rate, the on-chain fee undercharges the sender relative to the true Ethereum gas + reward cost, letting users push cheap messages through the queue while relayers are reimbursed below actual cost — a public-underpriced-work condition that can starve/relayer-disincentivize delivery and stall bridge processing, mirroring the "deposit before `updateRSETHPrice`" front-running window in the report.

### Finding Description
`calculate_fee` computes the DOT-denominated cost of delivering a message to Ethereum by dividing the ether-denominated remote fee by `params.exchange_rate`: [1](#0-0) 

`params` is `T::PricingParameters::get()` — a plain storage value written only by the privileged `set_pricing_parameters` extrinsic in the system pallet: [2](#0-1) 

The module doc explicitly documents this as a manual, periodic, non-live oracle: [3](#0-2) 

This is structurally identical to the `LRTOracle` bug class: a single stored exchange rate consumed by a public, unprivileged entry path (message submission / fee charging for outbound delivery), refreshed only by an out-of-band, delayed, manually-triggered update. Unlike `pallet-nomination-pools`, where `points_to_balance`/`balance_to_point` are derived live from `T::StakeAdapter::active_stake()` on every call (so slashes are reflected immediately, defeating a stale-rate attack — see `substrate/frame/nomination-pools/src/lib.rs:1064-1078` and `on_slash` at `substrate/frame/nomination-pools/src/lib.rs:4341-4373`), the Snowbridge pricing value has no live feed and is not automatically kept in sync with ETH/DOT market movement, so a real price move (e.g. ETH appreciates or gas spikes) is not reflected until governance calls `set_pricing_parameters` — a window that can span days ("every few weeks" per the pallet's own docs).

Existing guards do not close this gap:
- `PricingParameters::validate()` only checks fields are non-zero, not that they are fresh or bounded to real market data (`bridges/snowbridge/primitives/core/src/pricing.rs:39-56`).
- `calculate_fee`'s `multiplier` is a static safety factor set at the same time as `exchange_rate`, so it does not compensate for drift that occurs *after* the last update — it only cushions volatility anticipated at update time, not unanticipated moves afterward.
- There is no per-message freshness check, decay, or automatic re-pricing tied to block time or an external price feed.

### Impact Explanation
When the real ETH/DOT rate moves such that `exchange_rate` overstates DOT's value relative to ETH (i.e., DOT has actually weakened), `calculate_fee` computes an artificially low DOT fee for delivering messages that require a fixed ETH-denominated reward and gas refund on the Ethereum side. Any account can then flood the outbound queue with underpriced messages (public, unprivileged entrypoint — messages reach `do_process_message` via XCM without special permissions). Relayers who must front real ETH gas on Ethereum will not be adequately compensated by `reward`/`fee_per_gas` computed against the outdated rate, discouraging relaying and stalling bridge message processing — directly matching the required impact class "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
Likelihood is moderate: it requires only normal market volatility in the ETH/DOT rate (no attacker collusion, no validator/relayer/prover compromise, no leaked keys) combined with the multi-day/weekly cadence of governance updates that the pallet documentation itself acknowledges. Any ordinary user can trigger the underpriced path simply by submitting messages during the stale window; no privileged action or malicious insider is required to *exploit* the condition (only the routine, non-malicious act of governance updating the rate on a lag).

### Recommendation
- Reduce the trust window on the cached exchange rate: either integrate a live/frequently-refreshed price source (as the pallet's own doc suggests is the long-term plan via ETH/DOT liquidity pools) or add a maximum-staleness check that halts/pauses `do_process_message` fee acceptance if `set_pricing_parameters` has not been called within a bounded number of blocks.
- Add automatic adjustment logic (e.g., periodic on-chain repricing bounded by a decay/EMA function) so `exchange_rate` cannot silently diverge from market reality for extended periods.
- Consider dynamically scaling the `multiplier` based on time-since-last-update to widen the safety margin as staleness grows.

### Proof of Concept
1. Governance sets `PricingParameters { exchange_rate: R0, fee_per_gas: F0, rewards, multiplier: M0 }` via `EthereumSystem::set_pricing_parameters` (`bridges/snowbridge/pallets/system/src/lib.rs:316-334`).
2. Real-world ETH/DOT market rate subsequently moves such that R0 now overstates DOT purchasing power relative to ETH (DOT depreciates).
3. Because `exchange_rate` is not refreshed, `OutboundQueue::calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`) continues computing `RemoteFeeAdjusted = Multiplier * (RemoteFee / R0)`, which now understates the true DOT-equivalent cost of the fixed ETH-denominated `reward`/`fee_per_gas` refund.
4. Any account submits outbound-triggering XCM messages during this window; each message is queued and processed by `do_process_message` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:301-364`), reserving `reward` and `max_fee_per_gas` in ether terms while only charging the sender the stale, underpriced DOT fee computed at submission.
5. Relayers who deliver these messages on Ethereum are reimbursed based on the fixed ether-denominated `reward`/`max_fee_per_gas`, which — from the chain's economic perspective — was purchased too cheaply in DOT, degrading the incentive to relay and risking a backlog/stall of bridge message processing until governance manually updates `exchange_rate`.

Note: I could not verify from the available index whether there is a separate, more tightly time-bound repricing mechanism added later (e.g., an automated oracle feed) that supersedes this manual model in the current codebase revision; the documented design in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` explicitly describes this as an "interim measure," which supports the finding but should be confirmed against the latest pallet version in a full Devin session if further certainty is needed.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L46-58)
```rust
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

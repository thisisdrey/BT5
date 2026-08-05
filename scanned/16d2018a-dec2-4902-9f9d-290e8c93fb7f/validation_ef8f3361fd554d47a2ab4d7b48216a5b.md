## Finding: Coretime sale price decay is driven by the relay-chain block number, so a stall in the Coretime chain's own block production lets the very first purchase after resumption buy cores at an artificially collapsed price

### Title
Coretime broker sale price collapses to the discounted end-price after any Coretime-chain block-production stall because `sale_price` measures decay against the relay-chain clock, not against chain liveness - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
The external report describes an auction whose price decays continuously against wall-clock/oracle time while liquidations are blocked, so when the guard (sequencer downtime check) is lifted the first liquidator captures an unfairly decayed price and the protocol is left with bad debt. The Polkadot SDK analog is `pallet-broker`'s bulk Coretime sale: the sale price decays linearly ("leadin") as a function of the *relay chain* block number, which advances independently of whether the Coretime parachain itself is producing blocks. If the Coretime chain stalls block production for any reason, the decay clock keeps running from the relay chain's perspective, and the first extrinsic executed after the stall observes a relay-block delta that has jumped forward by the whole outage, collapsing the price straight to (or near) `end_price` with no market having had a chance to react.

### Finding Description
The sale price is computed purely as a function of the relay chain block number recorded in the sale and the current relay chain block number: [1](#0-0) 

`now` is obtained via `RCBlockNumberProviderOf::<T::Coretime>::current_block_number()`, which on the Coretime system parachain resolves to `RelaychainDataProvider`, a `BlockNumberProvider` that reads the relay-parent block number out of the *current parachain block's* validation data: [2](#0-1) 

This value only changes when the Coretime chain itself successfully authors a new block carrying fresh validation data. Nothing in `pallet-broker` tracks whether the chain was actually live and producing blocks during the leadin window — `do_purchase` only checks that the sale has started, not that the elapsed relay-block delta reflects real elapsed liveness: [3](#0-2) 

Because `sale.sale_start` and `leadin_length` are relay-chain block quantities (a deliberate design choice made in PR `#5656`, "Use Relay Blocknumber in Pallet Broker", to be "more future proof"): [4](#0-3) 

...the sale's price curve is bound to a clock the Coretime chain does not control the progression of at all. If the Coretime chain fails to get a block included for an extended period (relay-chain congestion, collator/validator issues, XCMP backpressure, or any other liveness disruption of this specific system parachain — none of which requires a malicious validator/collator, just ordinary liveness degradation), the relay chain continues to finalize blocks in the background. The moment the Coretime chain resumes block production, the very first block's validation data jumps `relay_parent_number` forward by the entire stall duration. The first `purchase`/`renew` extrinsit executed in that block then computes:

```
num = now.saturating_sub(sale.sale_start).min(leadin_length)
```

with `now` already far past `sale_start + leadin_length`, so `num` saturates at `leadin_length`, driving `leadin_factor_at(1.0)` and yielding the fully discounted `end_price` — exactly as if the sale had already been running at market price for its whole leadin period, even though no real price discovery/decay was observable to any buyer during the stall.

### Impact Explanation
This is the direct structural analog of the reported bug class: a decaying, publicly-purchasable price curve that keeps "running" against a clock unrelated to whether the protected activity (bidding/buying) was actually possible, so that recovery from downtime is monetized by whoever transacts first. Here the effect is that Coretime — a scarce, protocol-critical resource used to schedule execution on the relay chain — can be bought by the first opportunistic buyer at the fully discounted price instead of the fair decayed price the market would have produced with continuous liveness. This is "public underpriced work" against the sale mechanism: it directly reduces protocol/network revenue from coretime sales (the parachain-slot analog of the original report's "bad debt"), and because Coretime purchases determine core scheduling, it can also let an attacker cheaply acquire coretime that should have cost substantially more, distorting the intended supply/demand price-adaptation (`AdaptPrice::adapt_price`) for future sales as well.

### Likelihood Explanation
No malicious validator, collator, relayer, or governance action is required — this is triggered purely by ordinary liveness disruption of the Coretime system parachain (which, unlike the relay chain, is not immune to stalls) combined with anyone submitting a purchase/renew transaction as soon as block production resumes. An attacker only needs to monitor the Coretime chain and have a purchase transaction ready to be included in the first post-stall block (or simply be first in the mempool/queue), which is a realistic and low-cost condition, mirroring how L2 sequencer restarts are trivially observable by MEV/arbitrage bots in the original report.

### Recommendation
`sale_price` should not allow the leadin decay to advance for relay-chain blocks during which the Coretime chain itself did not produce a block (i.e., track elapsed *Coretime-chain* liveness, or cap the observed `now` progression per Coretime-chain block, similar to voiding/excluding downtime as recommended in the original report). Alternatively, persist the last block-number at which the chain successfully priced a purchase and clamp/interpolate `now` so that a single missed span of Coretime-chain blocks cannot advance the leadin position by more than the real number of Coretime-chain blocks produced.

### Proof of Concept
1. `pallet-broker::do_start_sales` starts a sale with `leadin_length = L` relay blocks and `CenterTargetPrice`/`MinimumPrice` decay via `AdaptPrice::leadin_factor_at` [5](#0-4) .
2. Sale begins at `sale.sale_start = R0` (a relay-chain block number).
3. Shortly after, the Coretime parachain stops producing blocks for a duration equal to `L` or more relay-chain blocks (relay-chain congestion / liveness disruption of this specific system parachain). No purchases occur because no blocks are authored, but the relay chain keeps finalizing blocks, so the "true" relay-chain height keeps climbing.
4. When the Coretime chain resumes and authors its next block, `set_validation_data` records `relay_parent_number = R0 + L (or more)`.
5. The first `Broker::purchase` extrinsic in that block calls `Self::sale_price(&sale, now)` with `now - sale_start >= leadin_length`, so `num` saturates and `leadin_factor_at(1.0)` returns the minimum multiplier — `sale_price` returns `end_price` (fully discounted) instantly, even though no gradual price discovery/decay ever occurred during the actual downtime, and even though (per the original report's pattern) an "ordinary" continuously-live sale would have taken the full `L` blocks of real elapsed liveness to reach that price. The buyer captures the core at the fully-discounted end price with zero market exposure to the intervening decay window.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L62-66)
```rust
	pub fn sale_price(sale: &SaleInfoRecordOf<T>, now: RelayBlockNumberOf<T>) -> BalanceOf<T> {
		let num = now.saturating_sub(sale.sale_start).min(sale.leadin_length).saturated_into();
		let through = FixedU64::from_rational(num, sale.leadin_length.saturated_into());
		T::PriceAdapter::leadin_factor_at(through).saturating_mul_int(sale.end_price)
	}
```

**File:** cumulus/pallets/parachain-system/src/lib.rs (L2046-2055)
```rust
pub struct RelaychainDataProvider<T>(core::marker::PhantomData<T>);

impl<T: Config> BlockNumberProvider for RelaychainDataProvider<T> {
	type BlockNumber = relay_chain::BlockNumber;

	fn current_block_number() -> relay_chain::BlockNumber {
		ValidationData::<T>::get()
			.map(|d| d.relay_parent_number)
			.unwrap_or_else(|| Pallet::<T>::last_relay_block_number())
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L149-164)
```rust
	pub(crate) fn do_purchase(
		who: T::AccountId,
		price_limit: BalanceOf<T>,
	) -> Result<RegionId, DispatchError> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let mut sale = SaleInfo::<T>::get().ok_or(Error::<T>::NoSales)?;
		Self::ensure_cores_for_sale(&status, &sale)?;

		let now = RCBlockNumberProviderOf::<T::Coretime>::current_block_number();
		ensure!(now > sale.sale_start, Error::<T>::TooEarly);
		let price = Self::sale_price(&sale, now);
		ensure!(price_limit >= price, Error::<T>::Overpriced);

		let core = Self::purchase_core(&who, price, &mut sale)?;

		SaleInfo::<T>::put(&sale);
```

**File:** prdoc/stable2503/pr_5656.prdoc (L1-18)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Use Relay Blocknumber in Pallet Broker

doc:
  - audience: Runtime Dev
    description: |
      Changing `sale_start`, `interlude_length` and `leading_length` in `pallet_broker` to use relay chain block numbers instead of parachain block numbers.
      Relay chain block numbers are almost deterministic and more future proof.

crates:
  - name: pallet-broker
    bump: major
  - name: coretime-rococo-runtime
    bump: major
  - name: coretime-westend-runtime
    bump: major
```

**File:** substrate/frame/broker/src/adapt_price.rs (L110-117)
```rust
impl<Balance: FixedPointOperand> AdaptPrice<Balance> for CenterTargetPrice<Balance> {
	fn leadin_factor_at(when: FixedU64) -> FixedU64 {
		if when <= FixedU64::from_rational(1, 2) {
			FixedU64::from(100).saturating_sub(when.saturating_mul(180.into()))
		} else {
			FixedU64::from(19).saturating_sub(when.saturating_mul(18.into()))
		}
	}
```

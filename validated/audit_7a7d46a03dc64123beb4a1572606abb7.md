## Analysis

The external Hubble report's core broken invariant is: **a price value is read directly from storage and used to trigger an irreversible, value-transferring state change, without any check that the price is fresh or within a sane bound relative to real market conditions.**

The closest local analog in `paritytech/polkadot-sdk` (as vendored here) is in the Snowbridge outbound queue, where `PricingParameters` (an admin/governance-set "price feed" containing `exchange_rate`, `fee_per_gas`, and `rewards`) is read and used to compute the message's fee/reward, and the message is then irreversibly committed into the outbound queue (nonce advanced, message hashed into the Merkle root) **regardless of whether that price data reflects current Ethereum gas costs**. [1](#0-0) 

`calculate_fee` uses `T::PricingParameters::get()` directly with no freshness/staleness check — only a one-time `validate()` that merely rejects zero values at the time governance sets them: [2](#0-1) [3](#0-2) 

`do_process_message` computes `max_fee_per_gas` and `reward` straight from this potentially-stale `pricing_params`, then unconditionally advances `Nonce`, appends to `Messages`/`MessageLeaves`, and emits `MessageAccepted` — i.e., the queue state advances permanently before any check that the priced fee is sufficient for the message to actually be economically deliverable on Ethereum: [4](#0-3) 

`PricingParameters` is only updatable via a governance-restricted extrinsic (`set_pricing_parameters` in `pallets/system`), and there is no on-chain mechanism enforcing that it be refreshed on any schedule or bounded against a live price feed — it is exactly the "print a stale/outlier price and let it silently drive irreversible on-chain settlement" pattern from the Hubble bug, just applied to bridge fee/reward pricing instead of collateral liquidation pricing. [5](#0-4) 

### Title
Outbound bridge messages are irreversibly queued using stale/unchecked `PricingParameters`, allowing fee starvation that permanently stalls Ethereum-bound delivery - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::calculate_fee` and `do_process_message` in the Snowbridge outbound-queue pallet read `T::PricingParameters::get()` — a governance-set "price oracle" for ETH gas price, DOT/ETH exchange rate, and relayer reward — and use it directly to compute the fee/reward for a message, then commit that message into the outbound queue (incrementing `Nonce`, appending to `Messages` and `MessageLeaves`, emitting `MessageAccepted`) with no check that the pricing data is fresh or that the computed reward/fee is still sufficient to economically justify delivery on Ethereum at current gas prices. This mirrors the Hubble Oracle bug: a price value is trusted and used to drive an irreversible on-chain state change without any freshness or bounds check.

### Finding Description
`calculate_fee` (lines 366-393) computes `params.fee_per_gas`, `params.multiplier`, and `params.exchange_rate` into a local fee, and separately the remote `reward` is taken verbatim from `pricing_params.rewards.remote` (line 337) and embedded into the `CommittedMessage` that gets Merkle-committed to Ethereum. The only validation ever performed on `PricingParameters` is `PricingParameters::validate()`, which rejects only exact-zero values at the moment governance sets them — it has no concept of staleness, no timestamp, and no comparison against a live/TWAP price: [2](#0-1) 

`do_process_message` then unconditionally advances the pallet's committed state — incrementing per-channel `Nonce`, appending the message and its hash to `Messages`/`MessageLeaves`, and emitting `MessageAccepted` — using whatever `pricing_params` happens to be in storage at that instant, with no check that the computed `reward`/`max_fee_per_gas` is still adequate for real-world Ethereum gas prices: [4](#0-3) 

Because commitment into the Merkle-committed queue is irreversible (nonces only increase, and the committed message is what Ethereum-side relayers must eventually deliver against), any period during which real Ethereum gas costs exceed what `PricingParameters` implies produces messages that are permanently under-rewarded relative to the actual cost of relaying them. No governance abuse is required to trigger the harm — it results purely from the absence of a staleness/bounds check on the pricing value used for a state-advancing action, exactly the pattern flagged in the Hubble finding (`Oracle.getUnderlyingPrice` returning a bogus/stale price that is then trusted for `isLiquidatable`).

### Impact Explanation
If `fee_per_gas`/`exchange_rate` lag a real spike in Ethereum gas prices (or a sustained ETH/DOT price move), every message accepted during that window is committed with a `reward`/`max_fee_per_gas` too low to make relaying profitable on Ethereum. Since queue advancement (`Nonce`, `Messages`, `MessageLeaves`) is irreversible and does not gate on price sufficiency, these messages sit undeliverable, degrading or stalling the outbound bridge processing pipeline — matching the "public underpriced work that degrades block production or stalls bridge processing" and "message queues ... must only advance after ... settlement succeed atomically" impact classes. Any user can trigger acceptance of underpriced messages into the permanent queue during a stale-price window simply by sending ordinary outbound messages; no privileged actor is needed to cause the harm (governance's role is passive/reactive, not the root cause).

### Likelihood Explanation
`PricingParameters` is a slow-moving, manually-updated governance value while Ethereum gas prices and ETH/DOT exchange rates are fast-moving and can spike sharply within the time between two governance updates. This is a foreseeable and recurring condition (analogous to Chainlink price staleness during market volatility), and the outbound-queue pallet performs zero on-chain freshness or sanity checking of `PricingParameters` before using it to drive irreversible queue state changes, so the vulnerable window opens naturally without any attacker action to force it, and is regularly reachable by ordinary usage.

### Recommendation
Add a staleness/bounds check on `PricingParameters` analogous to Oracle price-freshness mitigations: track a last-updated timestamp/block for `PricingParameters` and reject (or hold/refund) message commitment via `do_process_message` if the parameters are older than an acceptable threshold, or bound accepted `fee_per_gas`/`exchange_rate` against an independently-verifiable live reference before allowing `Nonce`/`Messages`/`MessageLeaves` to advance. Alternatively, allow re-pricing of already-queued-but-undelivered messages so stale pricing cannot permanently strand them in the committed queue.

### Proof of Concept
1. Governance sets `PricingParameters{ fee_per_gas: X, exchange_rate: R, rewards.remote: Y }` via the `pallet-system` governance extrinsic.
2. Real Ethereum gas price spikes (or ETH/DOT market moves) such that `Y` wei is no longer sufficient to cover the real cost of a relayer submitting the delivery proof on Ethereum, but governance has not yet updated `PricingParameters`.
3. Any user sends a message that reaches `do_process_message`; the pallet computes `reward = pricing_params.rewards.remote` (stale/insufficient value) and `max_fee_per_gas = pricing_params.fee_per_gas` (stale value), builds the `CommittedMessage`, and unconditionally appends it to `Messages`/`MessageLeaves`, advances `Nonce`, and emits `MessageAccepted` — see [4](#0-3)  — with no check comparing the reward against real-time relay cost.
4. The message is now permanently committed in the Merkle-rooted queue with an economically insufficient reward; relayers have no incentive to deliver it, and because nonces only advance forward with no re-pricing mechanism, the message (and potentially the channel behind it, depending on ordering guarantees) stalls indefinitely until governance intervenes — demonstrating fund-lock/stall impact stemming directly from the unchecked use of a stale price value, exactly as in the Hubble Oracle finding.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-364)
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

			// ABI-encode and hash the prepared message
			let message_abi_encoded = ethabi::encode(&[message.clone().into()]);
			let message_abi_encoded_hash = <T as Config>::Hashing::hash(&message_abi_encoded);

			Messages::<T>::append(Box::new(message));
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			Self::deposit_event(Event::MessageAccepted { id: queued_message.id, nonce });

			Ok(true)
		}
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L1-20)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>
//! Governance API for controlling the Ethereum side of the bridge
//!
//! # Extrinsics
//!
//! ## Governance
//!
//! Only Polkadot governance itself can call these extrinsics. Delivery fees are waived.
//!
//! * [`Call::upgrade`]`: Upgrade the gateway contract
//! * [`Call::set_operating_mode`]: Update the operating mode of the gateway contract
//!
//! ## Polkadot-native tokens on Ethereum
//!
//! Tokens deposited on AssetHub pallet can be bridged to Ethereum as wrapped ERC20 tokens. As a
//! prerequisite, the token should be registered first.
//!
//! * [`Call::register_token`]: Register a token location as a wrapped ERC20 contract on Ethereum.
#![cfg_attr(not(feature = "std"), no_std)]
```

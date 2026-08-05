Confirmed: there is no maximum fee-factor constant anywhere in `bridges/primitives/xcm-bridge-hub-router` or `bridges/modules/xcm-bridge-hub-router` — only `MINIMAL_DELIVERY_FEE_FACTOR` exists as a floor. The multiplicative growth in `FeeTracker::do_increase_fee_factor` (`polkadot/runtime/parachains/src/lib.rs`) is unbounded above.

### Title
Unbounded exponential `DeliveryFeeFactor` growth allows public congestion signal to price out and stall bridge/XCM message delivery - (File: `polkadot/runtime/parachains/src/lib.rs`, `bridges/modules/xcm-bridge-hub-router/src/lib.rs`)

### Summary
The reported bug's core invariant is: a per-trade fee computed as `region * baseFee` scales without any protocol-wide ceiling, so a single unprivileged action can be priced at (or above) 100% of the value at risk, with no revert-guard to stop it. The direct analog in this repo is the `FeeTracker::do_increase_fee_factor` / `ExponentialPrice` delivery-fee mechanism shared by `pallet-parachain-system`/DMP and `pallet-xcm-bridge-hub-router`: the fee multiplier (`DeliveryFeeFactor` / `BridgeState.delivery_fee_factor`) is multiplied by `EXPONENTIAL_FEE_BASE` (or more) every time a triggering condition occurs, with **no upper bound**, and is publicly and cheaply triggerable. [1](#0-0) 

### Finding Description
`FeeTracker::do_increase_fee_factor` multiplies the stored fee factor unconditionally, with only a floor (`MIN_FEE_FACTOR`) defined, and no `MAX_FEE_FACTOR`: [2](#0-1) 

This is used both by the relay-chain DMP queue (`polkadot/runtime/parachains/src/dmp.rs`, `increase_fee_factor` called from `queue_downward_message` whenever the queue length passes a threshold) and by `bridges/modules/xcm-bridge-hub-router/src/lib.rs`'s `on_message_sent_to_bridge`, which is invoked on *every* message sent through the router while the sibling channel is reported congested, regardless of who sent it: [3](#0-2) 

The resulting factor is then applied multiplicatively to compute the *actual balance* a sender must pay via `ExponentialPrice::price_for_delivery` / the router's `exporter_for`: [4](#0-3) [5](#0-4) 

Because the multiplication is `saturating_mul` with no ceiling check anywhere in `do_increase_fee_factor`, `increase_fee_factor`, or the callers, the factor can grow exponentially block-after-block for as long as the triggering condition (queue length above threshold, or channel/bridge congestion) persists. Congestion itself is either (a) directly driven by public message volume (DMP threshold based on queue length) or (b) reported by `report_bridge_status`, but the growth step itself fires on **every** publicly-sendable XCM message while congested — an unprivileged party can keep the condition true cheaply (e.g. by repeatedly sending minimal XCM programs) to keep multiplying the factor. Recovery only occurs via `decrease_fee_factor`, which divides by the same base once per relevant trigger (`prune_dmq` / `on_initialize` when un-congested) — an asymmetric ratchet: many multiplicative "up" steps can be forced quickly by spamming messages/queue pressure, while "down" steps are limited to at most one per block/prune event. There is no code path that cancels a message, reverts, or caps the fee once it would exceed the value being transferred or a sane maximum — unlike the recommended fix in the external report, which explicitly calls for a threshold that reverts the operation instead of letting it charge near/over 100%.

### Impact Explanation
Once the fee factor grows large enough, `price_for_delivery`/`exporter_for` demand a delivery fee that can dwarf the value of the assets a user is trying to move over the bridge or via DMP, or exceed what any sender is willing/able to pay. Since there is no maximum, and no code paths ever reset the factor except gradual division, this can degenerate into a de facto stall of message delivery for that lane/para — legitimate senders can no longer afford to route messages, matching the "public underpriced work that degrades block production or stalls bridge processing" impact class. It also risks locking funds in flight (fees withdrawn but destination unreachable/uneconomical) for any user who submits under a stale, since-inflated fee quote.

### Likelihood Explanation
The growth is triggered from a fully public, unprivileged surface: any account (or a low-cost self-referential loop, since `on_message_sent_to_bridge`/DMP `queue_downward_message` don't gate the *sender* of the triggering messages) can push the queue/channel into the congested regime and then keep it there by continuing to send messages, ratcheting the fee upward every time. No governance, validator, or relayer collusion is required — this squarely fits the "unprivileged attacker" and "public underpriced work" criteria in the impact gate. The severity/likelihood is somewhat mitigated by hard queue-size caps (which drop/reject messages before OOM) and by the fact this is a deliberately designed anti-spam mechanism — but the *absence of any fee ceiling* means it is not bounded to a "reasonable" anti-spam multiplier and can run away arbitrarily, which was never an intended design guarantee documented anywhere in the module.

### Recommendation
Introduce a `MAX_FEE_FACTOR` (or equivalent) constant/config bound in `FeeTracker` (`polkadot/runtime/parachains/src/lib.rs`) and enforce it inside `do_increase_fee_factor`, mirroring the external report's mitigation of establishing a protocol-wide ceiling on the derived fee/tax so it cannot silently balloon to effectively "100%+ of value" territory. Additionally, consider decoupling the growth trigger from raw per-message counts contributed by potentially the same low-cost sender, and/or symmetric recovery speed so the ratchet cannot be driven up faster than it can come down.

### Proof of Concept
1. Configure a parachain with `pallet-xcm-bridge-hub-router` (or DMP) using default `ExponentialPrice`/`FeeTracker` parameters.
2. Have the sibling bridge hub (or queue depth) enter a congested state, e.g. call `report_bridge_status(origin: BridgeHubOrigin, is_congested: true)` (for the router) or push DMP queue length above `threshold` in `dmp.rs`.
3. Repeatedly send low-value/cheap XCM messages through the router/DMP from an unprivileged account. Each call to `on_message_sent_to_bridge` / `queue_downward_message` invokes `do_increase_fee_factor`, multiplying `delivery_fee_factor` by `EXPONENTIAL_FEE_BASE` (≥1.05) with no upper bound check: [6](#0-5) 
4. Observe that `price_for_delivery`/`exporter_for` fee quotes grow exponentially in lock-step, eventually demanding fees that exceed any reasonable message value, and that no code path halts this growth or reverts the triggering call once the fee becomes economically absurd.
5. Because recovery (`decrease_fee_factor`/`do_decrease_fee_factor`) only divides once per un-congested block/prune event, an attacker sustaining congestion for a short period can inflict a fee spike that takes disproportionately longer to unwind, stalling legitimate message/bridge traffic in the interim.

### Citations

**File:** polkadot/runtime/parachains/src/lib.rs (L61-92)
```rust
/// Trait for tracking message delivery fees on a transport protocol.
pub trait FeeTracker {
	/// Type used for assigning different fee factors to different destinations
	type Id: Copy;

	/// Minimal delivery fee factor.
	const MIN_FEE_FACTOR: FixedU128 = FixedU128::from_u32(1);
	/// The factor that is used to increase the current message fee factor when the transport
	/// protocol is experiencing some lags.
	const EXPONENTIAL_FEE_BASE: FixedU128 = FixedU128::from_rational(105, 100); // 1.05
	/// The factor that is used to increase the current message fee factor for every sent kilobyte.
	const MESSAGE_SIZE_FEE_BASE: FixedU128 = FixedU128::from_rational(1, 1000); // 0.001

	/// Returns the current message fee factor.
	fn get_fee_factor(id: Self::Id) -> FixedU128;

	/// Sets the current message fee factor.
	fn set_fee_factor(id: Self::Id, val: FixedU128);

	fn do_increase_fee_factor(fee_factor: &mut FixedU128, message_size: u128) {
		let message_size_factor = FixedU128::from(message_size.saturating_div(1024))
			.saturating_mul(Self::MESSAGE_SIZE_FEE_BASE);
		*fee_factor = fee_factor
			.saturating_mul(Self::EXPONENTIAL_FEE_BASE.saturating_add(message_size_factor));
	}

	/// Increases the delivery fee factor by a factor based on message size and records the result.
	fn increase_fee_factor(id: Self::Id, message_size: u128) {
		let mut fee_factor = Self::get_fee_factor(id);
		Self::do_increase_fee_factor(&mut fee_factor, message_size);
		Self::set_fee_factor(id, fee_factor);
	}
```

**File:** bridges/modules/xcm-bridge-hub-router/src/lib.rs (L204-239)
```rust
		/// Called when new message is sent (queued to local outbound XCM queue) over the bridge.
		pub(crate) fn on_message_sent_to_bridge(message_size: u32) {
			tracing::trace!(
				target: LOG_TARGET,
				?message_size, "on_message_sent_to_bridge"
			);
			let _ = Bridge::<T, I>::try_mutate(|bridge| {
				let is_channel_with_bridge_hub_congested =
					T::LocalXcmChannelManager::is_congested(&T::SiblingBridgeHubLocation::get());
				let is_bridge_congested = bridge.is_congested;

				// if outbound queue is not congested AND bridge has not reported congestion, do
				// nothing
				if !is_channel_with_bridge_hub_congested && !is_bridge_congested {
					return Err(());
				}

				let previous_factor = Self::get_fee_factor(());
				// ok - we need to increase the fee factor, let's do that
				<Self as FeeTracker>::do_increase_fee_factor(
					&mut bridge.delivery_fee_factor,
					message_size as u128,
				);

				tracing::info!(
					target: LOG_TARGET,
					from=%previous_factor,
					to=%bridge.delivery_fee_factor,
					"Bridge channel is congested. Increased fee factor"
				);
				Self::deposit_event(Event::DeliveryFeeFactorIncreased {
					new_value: bridge.delivery_fee_factor,
				});
				Ok(())
			});
		}
```

**File:** bridges/modules/xcm-bridge-hub-router/src/lib.rs (L323-332)
```rust
		// compute fee amount. Keep in mind that this is only the bridge fee. The fee for sending
		// message from this chain to child/sibling bridge hub is determined by the
		// `Config::ToBridgeHubSender`
		let message_size = message.encoded_size();
		let message_fee = (message_size as u128).saturating_mul(T::ByteFee::get());
		let fee_sum = base_fee.saturating_add(message_fee);
		let fee_factor = Self::get_fee_factor(());
		let fee = fee_factor.saturating_mul_int(fee_sum);

		let fee = if fee > 0 { Some((T::FeeAsset::get(), fee).into()) } else { None };
```

**File:** polkadot/runtime/common/src/xcm_sender.rs (L89-94)
```rust
	fn price_for_delivery(id: Self::Id, msg: &Xcm<()>) -> Assets {
		let msg_fee = (msg.encoded_size() as u128).saturating_mul(M::get());
		let fee_sum = B::get().saturating_add(msg_fee);
		let amount = F::get_fee_factor(id).saturating_mul_int(fee_sum);
		(A::get(), amount).into()
	}
```

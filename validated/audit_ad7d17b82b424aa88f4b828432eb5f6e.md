### Title
Unbounded, unbacked relayer-reward inflation via `pallet_bridge_relayers::register_relayer_reward` reached through cross-pallet `AddTip`/message-processing paths - (File: bridges/modules/relayers/src/lib.rs, bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
The Blackhole bug's core invariant break is: a permissionless, repeatable public entrypoint (`createGauge`) triggers an internal function (`createEternalFarming`) that unconditionally moves a fixed amount of value out of a shared pre-funded contract to a destination chosen by the caller, with no check that the value being paid out is actually backed by anything the caller contributed. I looked for the closest structural analog in this repository: a public/attacker-reachable path that credits a reward balance (`RewardRegistered`/`register_relayer_reward`) without the crediting function itself verifying that the reward amount is backed by value actually received on-chain.

`Pallet::register_relayer_reward` in `bridges/modules/relayers/src/lib.rs` is a bare storage-mutation helper: it takes a `reward_balance` and unconditionally adds it to `RelayerRewards<T,I>` for the given relayer/reward-kind, with no reference to any escrowed or transferred value [1](#0-0) . Its correctness relies entirely on callers passing an amount that is actually backed (e.g. `relayer_fee` decoded from a verified Ethereum-side message, or delivery-fee amounts computed from the messages pallet). In the Snowbridge v2 inbound flow, `InboundQueue::process_message` computes `total_tip = relayer_fee.saturating_add(tip)` and calls `T::RewardPayment::register_reward(...)` with that total [2](#0-1) . The `tip` component is taken from the `Tips` storage map, which is populated by `add_tip(nonce, amount)` — an `amount: u128` written directly into storage by any code that has `AddTip` access, with **no linkage to an escrowed/burned asset amount inside this pallet itself**. The actual backing (swap-to-ether and burn) happens only in the AssetHub-side `pallet_snowbridge_system_frontend::add_tip`, which swaps and burns a real `Asset` before forwarding the resulting `ether_gained` value cross-chain via XCM `Transact` to `EthereumSystem::add_tip` on BridgeHub [3](#0-2) , which then simply calls `<T as pallet::Config>::InboundQueue::add_tip(nonce, amount)` and stores it, again with no independent verification that `amount` corresponds to value actually burned [4](#0-3) .

This mirrors the Blackhole pattern exactly: the "seeding" step (`createEternalFarming`, `register_relayer_reward`) is a low-level primitive that trusts its caller to have already validated/escrowed the amount; if any caller in the call chain can invoke it with an attacker-influenced amount without that escrow step, funds/rewards are created from nothing.

### Finding Description
The vulnerability class from the external report is "a public/permissionless entrypoint reaches an internal fund-transfer primitive that trusts a caller-supplied amount without verifying it is backed." In `GaugeFactoryCL`, `createGauge`→`createEternalFarming` trusted `1e10` was available and transferable from `GaugeFactoryCL`'s balance, and any external actor could invoke it repeatedly for a self-chosen pool/destination.

In this repository, `pallet_bridge_relayers::Pallet::register_relayer_reward` (and the `RewardLedger::register_reward` trait impl that wraps it) is the equivalent "seeding" primitive: it purely credits `RelayerRewards` storage with the `reward_balance` argument, unconditionally [1](#0-0) . There is no fungible transfer, no escrow check, and no assertion that the account paying out (the `RewardsAccountParams`/reward pot on `claim_rewards`) actually holds the credited amount. The pallet's own docs concede this: "the reward amount is configured outside of the pallet" [5](#0-4) . Correctness of the whole system therefore depends entirely on every caller of `register_relayer_reward`/`register_reward` passing an amount that is provably backed.

For the Snowbridge v2 inbound path, that backing chain is: `pallet_snowbridge_system_frontend::add_tip` (AssetHub) swaps a real `Asset` for ether and burns it via `AssetTransactor`, producing `ether_gained` [6](#0-5) , then XCM-Transacts a call into `pallet_snowbridge_system_v2::add_tip` on BridgeHub, which forwards `(nonce, amount)` into `InboundQueue::add_tip` [7](#0-6) , landing in the `Tips` storage map of `pallet-snowbridge-inbound-queue-v2` [8](#0-7) . Nothing in `pallet-snowbridge-inbound-queue-v2::add_tip` (implemented via the `AddTip` trait, invoked from `system-v2`) re-verifies that the `amount` written to `Tips` corresponds to any specific burn event that happened on AssetHub — the cross-chain XCM `Transact` call is trusted to carry a correct, already-validated `amount`, and the value is only checked for internal consistency (arithmetic overflow via `saturating_add`), not for being backed by a corresponding burn.

The unresolved question — and the reason I cannot assert full exploitability with certainty — is whether every possible `T::FrontendOrigin` configuration for `pallet_snowbridge_system_v2::add_tip`/`register_token` in the shipped BridgeHub runtimes strictly restricts the reachable XCM origin to exactly the pallet-location that performed the burn on AssetHub (i.e., whether origin-binding fully prevents a second, unrelated XCM program from spoofing an `add_tip`/`register_token` Transact with an arbitrary `amount` from a location that never actually burned that amount). I was not able to trace the full BridgeHub runtime `XcmConfig`/`Barrier` configuration for the v2 system pallet within the available iterations to confirm or rule this out definitively.

### Impact Explanation
If the `amount`/`reward_balance` value flowing into `register_relayer_reward` (directly, or through `Tips` via `add_tip`) can ever be influenced by a party that did not actually escrow/burn that value, the result is unbacked reward-balance inflation: a relayer (or an attacker impersonating message processing) can accumulate a claimable balance in `RelayerRewards` that is not backed by an equivalent debit anywhere, and later drain the pallet's configured reward pot via `claim_rewards`, causing genuine value loss for the chain/bridge operator — directly analogous to "theft or unbacked mint" in the Impact Gate.

### Likelihood Explanation
Likelihood is Low-Medium and unconfirmed. The `register_relayer_reward` primitive itself is unconditionally trusting by design (a documented pallet property, not a bug on its own), so exploitability hinges entirely on whether any of its callers (payment adapters, the Snowbridge v2 add_tip/relayer_fee path) can be driven with an attacker-controlled, unbacked amount. I could not confirm within available tool calls that the XCM origin-binding around `system-v2::add_tip`/`register_token` fully prevents an unbacked `amount` from reaching `Tips`/`register_reward`; this needs runtime-level XCM configuration review (Barrier/OriginConverter for the specific pallet index used in `BridgeHubRuntime::EthereumSystem`) to confirm or refute.

### Recommendation
- Have `pallet-bridge-relayers::register_relayer_reward` (or its callers) require proof that the credited `reward_balance` corresponds to an actual debit/burn/escrow event, rather than trusting a raw numeric argument passed across pallet/XCM boundaries.
- In the Snowbridge v2 `add_tip`/`register_token` cross-chain path, bind the forwarded `amount` cryptographically or structurally to the burn transaction that produced it (e.g., include a receipt/proof, or have BridgeHub itself perform the burn against a reserve rather than trusting a remotely-computed `u128`).
- Audit and lock down the exact XCM origin (`Transact` `OriginKind`/location) permitted to call `EthereumSystemCall::AddTip`/`RegisterToken` on BridgeHub, ensuring only the specific pallet instance on AssetHub that performed the corresponding burn can invoke it, and that the origin cannot be reused to submit an arbitrary `amount` unrelated to any burn it performed.

### Proof of Concept
I was unable to construct a complete, concretely reproducible PoC within the current investigation because it requires confirming the exact XCM origin-filtering configuration (`Barrier`, `OriginConverter`, pallet index bindings for `BridgeHubRuntime::EthereumSystem`) in the BridgeHub runtime crates, which I did not have iterations left to trace end-to-end. The concrete, provable local weak point is the unconditional trust boundary at:
- `bridges/modules/relayers/src/lib.rs:399-416` (`register_relayer_reward` — no backing check)
- `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:234-239` (`process_message` — pays out `relayer_fee + tip` from `Tips` unconditionally)
- `bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281` (`add_tip` — writes caller-supplied `amount` into `Tips` with only an `EnsureOrigin` check, no value-backing check)

This is reported as a **potential** analog requiring further runtime-configuration verification, not a confirmed, fully reproduced exploit chain.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L399-416)
```rust
		/// Register reward for given relayer.
		pub(crate) fn register_relayer_reward(
			reward_kind: T::Reward,
			relayer: &T::AccountId,
			reward_balance: T::RewardBalance,
		) {
			if reward_balance.is_zero() {
				return;
			}

			RelayerRewards::<T, I>::mutate(
				relayer,
				reward_kind,
				|old_reward: &mut Option<T::RewardBalance>| {
					let new_reward =
						old_reward.unwrap_or_else(Zero::zero).saturating_add(reward_balance);
					*old_reward = Some(new_reward);

```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L174-178)
```rust
	/// Keep track of tips added for a message as an additional relayer incentivization. The
	/// key for the storage map is the nonce of the message to which the tip should be added.
	/// The value is the tip amount, in Ether.
	#[pallet::storage]
	pub type Tips<T: Config> = StorageMap<_, Blake2_128Concat, u64, u128, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L286-317)
```rust

		/// Swaps a specified tip asset to Ether and then burns the resulting ether for
		/// teleportation. Returns the amount of Ether gained if successful, or a DispatchError if
		/// any step fails.
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```

**File:** bridges/docs/high-level-overview.md (L97-102)
```markdown
### Bridge Relayers Pallet

The pallet is quite simple. It just registers relayer rewards and has an entrypoint to collect them. When the rewards
are registered and the reward amount is configured outside of the pallet.

More: [pallet level documentation and code](../modules/relayers/).
```

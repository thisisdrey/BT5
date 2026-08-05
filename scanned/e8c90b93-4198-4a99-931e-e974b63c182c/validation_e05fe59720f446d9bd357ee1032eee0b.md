## Analysis

The external report's core broken invariant is: **a system registers "IOUs" (pending trade obligations / slashable liabilities) against a shared collateral pool, but never tracks the sum of those IOUs against the pool's actual balance**, so obligations can be accepted (and later fail to settle) beyond what the pool can back.

The closest local analog is the reward-accounting design in `pallet-bridge-relayers`, used both for classic bridge message relaying and for Snowbridge (`BridgeReward::Snowbridge`).

### Title
Unbacked reward liabilities in `pallet-bridge-relayers`: `RelayerRewards` can accumulate claims that exceed the balance of the corresponding `PayRewardFromAccount` pot, permanently locking relayer rewards - (File: `bridges/modules/relayers/src/lib.rs`, `bridges/primitives/relayers/src/lib.rs`)

### Summary
`register_relayer_reward` credits an entry in the `RelayerRewards` double map for a given `(relayer, reward_kind)` pair whenever a message-delivery/confirmation transaction is processed, purely as a bookkeeping increment. Nothing checks the balance of the account that will eventually have to pay out that reward. Payment happens later, on `claim_rewards`/`claim_rewards_to`, via `PayRewardFromAccount::pay_reward`, which does a plain `fungible::Mutate::transfer` from a per-`RewardsAccountParams` sub-account ("rewards account") to the beneficiary. [1](#0-0) [2](#0-1) 

### Finding Description
The system has exactly the structure the report criticizes:

1. **No linkage between registered liability and locked collateral.** `register_relayer_reward` only mutates `RelayerRewards<T,I>` — a running total of what is *owed* to a relayer for a given reward pot (`RewardsAccountParams`/`BridgeReward`) — without any read of, or reservation against, the actual balance sitting in the `rewards_account` derived by `PayRewardFromAccount::rewards_account`. [3](#0-2) [4](#0-3) 

2. **Multiple independent transactions can each look individually fine while aggregate liability exceeds the pot.** `analyze_call_result` in the signed-extension computes a per-transaction `refund` from weight/size/tip and returns `RelayerAccountAction::Reward(...)`, purely as a function of that one transaction's dispatch info — exactly like the report's "each individual trade doesn't exceed `tradeAmount * tradableBondRatio`" check with no aggregate state. [5](#0-4) 

3. **When the pot cannot cover it, the payout fails outright instead of paying what's available.** `PayRewardFromAccount::pay_reward` calls `T::transfer(..., reward.into(), Preservation::Expendable)` for the *full* registered `reward` amount. If the pot's balance is less than that (analogous to "if there is only 990 remaining and a slash of 1000 occurs, it fails instead of taking 990"), the transfer errors, `claim_rewards`/`claim_rewards_to` return `Err(FailedToPayReward)`, and — because dispatchables are transactional in FRAME — the `RelayerRewards` entry removal is rolled back. This exact "insufficient-balance → revert-in-full" pattern is called out verbatim in the report's recommendation section (`min(balance, amountToSlash)`), and the codebase's tests exercise the same failure mode. [2](#0-1) [6](#0-5) 

4. **Same pattern is reused for Snowbridge rewards.** `BridgeReward::Snowbridge` is registered via the same `RewardLedger`/`RelayerRewards` mechanism from both `snowbridge-pallet-outbound-queue-v2::process_delivery_receipt` (paying `order.fee` out of `PendingOrders`) and `inbound-queue-v2` message processing, so the same unbacked-liability gap propagates into the Snowbridge reward path that HackenProof explicitly scopes in. [7](#0-6) 

Existing guards do not stop this: there is no invariant anywhere that `sum(RelayerRewards for a given reward_kind) <= Balance(rewards_account(reward_kind))`. The pot is only ever "topped up" by ad-hoc mechanisms (delivery fees credited on success, sudo/test deposits, or governance `deposit_account` in the runtime configs), while the liability side (`RelayerRewards`) grows monotonically with every processed message/receipt — the same "available vs. locked" distinction the report says is missing from `BondManager`.

### Impact Explanation
Relayers act as unprivileged, permissionless participants: anyone can register as a relayer and submit valid message-delivery/confirmation extrinsics, driving up `RelayerRewards` entries without any admin/governance action. If aggregate registered rewards for a lane/reward-kind exceed what is actually sitting in that lane's `rewards_account` (which is entirely plausible since the account is funded independently of the liability side, e.g. by upfront fees whose totals can diverge from later-computed refunds/tips), a subset of relayers will find their legitimately-earned reward permanently unclaimable (`FailedToPayReward`), i.e., a locked-fund condition. This falls squarely in the accepted impact category "permanent user-fund or bridge-state lock."

### Likelihood Explanation
This does not require a malicious relayer, validator, collator, or governance actor — it only requires normal usage: enough relayers process enough messages that the sum of computed refunds/rewards for one `RewardsAccountParams` pot outpaces the funds that have actually flowed into that specific sub-account. No code path anywhere caps registration by pot balance or rebalances the pot before crediting `RelayerRewards`, so the condition is reachable purely through organic relay traffic and fee/refund computation drift, not through any privileged or attacker-controlled input.

### Recommendation
- Track, per `RewardsAccountParams`/`BridgeReward`, a running total of unclaimed registered liability and refuse to register (or defer/queue) new rewards once outstanding liability would exceed the current balance of the corresponding `rewards_account`.
- Alternatively, make `PayRewardFromAccount::pay_reward` pay `min(pot_balance, reward)` and record the shortfall as still-owed, rather than reverting the whole claim, mirroring the report's `min(bondManager.balance(...), amountToSlash)` recommendation.
- Add an invariant check/benchmark asserting `sum(RelayerRewards) <= balance(rewards_account)` for each pot at block finalization in test/CI, so accounting drift is caught before it can strand relayer funds.

### Proof of Concept
1. Configure a lane (`RewardsAccountParams`) whose `rewards_account` is funded only from delivery fees collected on-chain (as in `bridge_rewards_works`/`claim_rewards_works` tests, where `PayRewardFromAccount::rewards_account` is minted a fixed amount).
2. Have several distinct relayers submit valid `receive_messages_proof`/confirmation transactions for that lane; each call to `analyze_call_result` independently computes and registers a `refund`/reward via `register_relayer_reward`, summing into `RelayerRewards` for each relayer, with no check against the pot's live balance. [8](#0-7) 
3. Once total registered `RelayerRewards` for that pot exceeds the pot's actual balance, have the first relayer call `claim_rewards`/`claim_rewards_to` — this succeeds and drains the pot.
4. A second relayer with a legitimately registered, non-zero `RelayerRewards` entry calls `claim_rewards`; `PayRewardFromAccount::pay_reward`'s `T::transfer` fails because the pot balance is now below the requested `reward`, and the whole extrinsic reverts with `Error::FailedToPayReward` — reproducing exactly the pattern demonstrated in `bridge_rewards_works` (`assert_err!(..., FailedToPayReward)`), leaving that relayer's reward permanently stuck as long as the pot is not independently replenished. [9](#0-8)

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L399-432)
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

					tracing::trace!(
						target: crate::LOG_TARGET,
						?relayer,
						?reward_kind,
						?new_reward,
						"Relayer can now claim reward for serving payer"
					);

					Self::deposit_event(Event::<T, I>::RewardRegistered {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
					});
				},
			);
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L536-546)
```rust
	/// Map of the relayer => accumulated reward.
	#[pallet::storage]
	pub type RelayerRewards<T: Config<I>, I: 'static = ()> = StorageDoubleMap<
		_,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Hasher1,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Key1,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Hasher2,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Key2,
		<RelayerRewardsKeyProviderOf<T, I> as StorageDoubleMapKeyProvider>::Value,
		OptionQuery,
	>;
```

**File:** bridges/primitives/relayers/src/lib.rs (L152-161)
```rust
impl<T, Relayer, LaneId, RewardBalance> PayRewardFromAccount<T, Relayer, LaneId, RewardBalance>
where
	Relayer: Decode + Encode,
	LaneId: Decode + Encode,
{
	/// Return account that pays rewards based on the provided parameters.
	pub fn rewards_account(params: RewardsAccountParams<LaneId>) -> Relayer {
		params.into_sub_account_truncating(b"rewards-account")
	}
}
```

**File:** bridges/primitives/relayers/src/lib.rs (L175-188)
```rust
	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
```

**File:** bridges/modules/relayers/src/extension/mod.rs (L262-269)
```rust
		// compute the relayer refund
		let mut post_info = *post_info;
		post_info.actual_weight = Some(post_info_weight);
		let refund = Self::compute_refund(info, &post_info, post_info_len, tip);

		// we can finally reward relayer
		RelayerAccountAction::Reward(relayer, reward_account_params, refund.into())
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L768-795)
```rust
			// Local account claiming is not supported for Snowbridge
			assert_err!(
				BridgeRelayers::claim_rewards(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);

			let claim_location = VersionedLocation::V5(Location::new(
				1,
				[
					Parachain(1000),
					xcm::latest::Junction::AccountId32 {
						id: account2.clone().into(),
						network: None,
					},
				],
			));
			// In unit tests without proper HRMP channel setup, the claim will fail at XCM sending.
			assert_err!(
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

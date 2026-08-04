I found a strong local analog to the `donateAndClaim()` bug: `PayAccountOnLocation::pay_reward` in Snowbridge's reward payment procedure charges the XCM delivery fee for the reward-mint message **from the claiming relayer**, not from the reward pool being paid out — meaning the actual value transferred to the relayer/beneficiary is not decoupled from the fee-payer's own balance, unlike the report's core issue (a wrapper forwarding a single `value`/fee to two different downstream operations without properly separating "amount to move" vs "fee to pay").

### Title
`claim_rewards_to()` charges XCM delivery fee to the claiming relayer instead of from the reward being claimed - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`pallet_bridge_relayers::claim_rewards_to` invokes `PayAccountOnLocation::pay_reward`, which builds an XCM to mint the relayer's reward on AssetHub and charges the **delivery fee** for that XCM via `XcmExecutor::charge_fees(relayer.clone(), fee)` — i.e., debited from the calling relayer's own local account balance — while the reward amount itself is separately encoded into the XCM as a `ReserveAssetDeposited` amount to be delivered to the `beneficiary`. This mirrors the `donateAndClaim()` flaw: two distinct value flows (the "payload" being claimed, and the "fee" required to deliver/settle it) are conflated/handled inconsistently within a single claim entrypoint, and the fee-charging step is not validated against the actual `msg.value`/fee provided by the caller in the extrinsic itself (there is none — it's an ordinary signed extrinsic paying only its transaction fee).

### Finding Description
`claim_rewards_to` in [1](#0-0)  is a public, unprivileged, signed extrinsic. It calls `do_claim_rewards`, which atomically removes the accrued `RelayerRewards` entry and calls `T::PaymentProcedure::pay_reward` [2](#0-1) .

For the Snowbridge reward kind, `PaymentProcedure` is implemented by `PayAccountOnLocation::pay_reward` [3](#0-2) . This function:
1. Builds an XCM containing `ReserveAssetDeposited(assets)` where `assets` is the reward balance being claimed, targeted at the caller-chosen `beneficiary` location.
2. Calls `validate_send::<XcmSender>(AssetHubLocation::get(), xcm)` to get a delivery `fee`.
3. Calls `XcmExecutor::charge_fees(relayer.clone(), fee)` — charging the **relayer's own account**, not the reward pool, for the delivery fee.
4. Delivers the XCM.

This is structurally identical to the report's core defect: the function that "moves value" (the reward) and the function that "pays for delivery" (the fee) are bundled into one call, but the fee is silently sourced from an account/balance that the caller did not explicitly provision inside the same call — in the Solidity report, `msg.value` covers `donate()` but not `claim()`'s LZ fee; here, the relayer's account is expected to have a sufficient free/transferable balance to cover the delivery fee at the time of calling `claim_rewards_to`, independent of and unrelated to the reward being claimed. If the relayer's balance is insufficient, `charge_fees` fails and the whole extrinsic reverts (this part is safe due to `try_mutate_exists` reverting the storage removal on `Err`) — so it is **not** a silent fund-loss bug, but a **built-in economic inconsistency**: an honest relayer must pre-fund an unrelated account balance to claim rewards it is legitimately owed, and the amount required (delivery fee, computed by `validate_send`) is fully attacker/market-controlled (a griefer can drive up delivery fees, e.g., via congestion or exchange-rate-based delivery pricing), potentially locking a relayer out of claiming legitimately owed rewards indefinitely if their liquid balance can't cover fees, distinct from the reward amount that would otherwise cover it.

### Impact Explanation
This does not directly enable theft, forged proofs, or unauthorized execution — the existing revert-on-failure semantics prevent double-spend or fund loss. The impact is limited to a liveness/availability issue for reward claiming (a relayer with insufficient local balance cannot claim earned rewards, since the fee is paid from their own account rather than from the reward being claimed), which is a lesser-severity finding than the original report's fund-loss framing.

### Likelihood Explanation
Any relayer calling `claim_rewards_to` with reward kind `Snowbridge` while holding insufficient balance to cover the current AssetHub delivery fee will trigger this. Likelihood is moderate — it depends on relayer balance management and delivery-fee market conditions, not on any privileged or malicious actor.

### Recommendation
Source the XCM delivery fee from the reward being claimed (deduct it from `reward_balance` before minting the remainder to the beneficiary) rather than charging it against the relayer's separate account balance, so the entire claim settles atomically and self-sufficiently from the claimed value, analogous to how the external report recommends explicitly supplying the LZ fee alongside the donation inside the same call.

### Proof of Concept
Given the code path is fully visible and deterministic, a concrete repro:
1. Relayer accrues `RelayerRewards` of `Snowbridge` kind (via `register_relayer_reward`, e.g. from Snowbridge inbound message processing).
2. Relayer's account has 0 (or very low) transferable balance (all funds are, e.g., held/locked, or simply nonexistent since rewards are minted on AssetHub, not on BridgeHub where the claim executes).
3. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, beneficiary)`.
4. `do_claim_rewards` removes the `RelayerRewards` entry, calls `pay_reward`, which calls `XcmExecutor::charge_fees(relayer, fee)` — this fails due to insufficient balance.
5. Whole extrinsic fails with `Error::FailedToPayReward` (matching the existing test `claim_snowbridge_rewards_to_local_account_fails` pattern in [4](#0-3) ), and the relayer cannot ever claim the reward until they separately fund their account with unrelated balance — an amount uncoupled from what they are owed.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
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

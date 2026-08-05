### Title
Relayer reward payout is marked settled after XCM send succeeds, not after execution/deposit succeeds on AssetHub - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`PayAccountOnLocation::pay_reward` (used as the `PaymentProcedure` for `BridgeReward::Snowbridge` in `bridge_common_config.rs`) treats a relayer reward as fully paid once the outbound XCM has been *sent* to AssetHub, without any confirmation that the `ReserveAssetDeposited`/`DepositAsset` instructions actually succeeded on the remote chain. This mirrors the ERC20 bug class in the report: the caller checks only the immediate "call succeeded" signal (here, `XcmSender::deliver`) and never verifies the real value movement (here, deposit into the beneficiary on AssetHub), so bookkeeping can advance ("reward paid") while the beneficiary receives nothing.

### Finding Description
`pay_reward` builds an unpaid, one-way XCM that reserve-mints Ether-denominated reward assets on AssetHub directly into the `beneficiary` location: [1](#0-0) 

The only checks performed are:
1. `validate_send::<XcmSender>` succeeds (message is well-formed/routable),
2. `XcmExecutor::charge_fees` succeeds locally on BridgeHub,
3. `XcmSender::deliver(ticket)` succeeds (message handed off to the transport layer).

None of these confirm that the `DepositAsset { assets: AllCounted(1).into(), beneficiary }` instruction actually executes successfully on AssetHub. If, on the destination side, the beneficiary location fails to resolve to an account (bad `Location`/junction), the account doesn't exist and can't receive the asset, a barrier/weight limit rejects part of the program, or the asset gets trapped (`AssetsTrapped`) for any other reason, the funds never reach the relayer even though `pay_reward` returned `Ok(())` on BridgeHub.

The caller, `Pallet::do_claim_rewards`, removes the `RelayerRewards` entry and emits `RewardPaid` as soon as `pay_reward` returns `Ok`: [2](#0-1) 

Because `RelayerRewards::<T, I>::try_mutate_exists` takes (removes) the reward balance before calling `pay_reward`, and the whole closure only reverts on `Err`, a local "send succeeded" result is indistinguishable from "value actually delivered." The settlement state (reward-claimed marker) advances irreversibly based on an unchecked intermediate signal exactly like the unchecked ERC20 `transfer`/`transferFrom` return value in the original report — the difference is that here the unchecked signal is "XCM accepted for delivery" rather than "token contract call returned true."

This is architecturally different from `PayRewardFromAccount` (the same-chain analog), which uses a real `fungible::Mutate::transfer` whose `Result` is authoritative and atomic with the extrinsic: [3](#0-2) 
There, if the transfer fails, `Err` propagates and the reward entry is not removed (transactional storage rollback). `PayAccountOnLocation` has no equivalent remote-execution acknowledgement/receipt mechanism, so it cannot offer the same atomicity guarantee, yet it is treated identically by the caller.

### Impact Explanation
A relayer whose beneficiary location cannot correctly accept the deposit on AssetHub (e.g., a malformed `Location`, a not-yet-existing account below ED, or any transient AssetHub-side XCM execution failure) permanently loses their earned reward: the reward is deleted from `RelayerRewards` on BridgeHub and `RewardPaid` is emitted, but no value is deposited on AssetHub (or it's trapped as `AssetsTrapped`). This is unbacked value destruction/loss from the relayer's perspective and a false "settled" state recorded on-chain — directly matching the "payout state must only advance after ... execution, and settlement succeed atomically" pivot.

### Likelihood Explanation
No malicious actor is required. Any legitimate relayer that supplies a beneficiary `Location` that AssetHub cannot resolve/deposit into (wrong junction encoding, insufficient existential deposit, or the destination-side `AllowUnpaidExecutionFrom`/barrier rejecting some part of the program) triggers the false-success path — this can happen through ordinary user error or transient conditions, since `claim_rewards_to` accepts an arbitrary `VersionedLocation` beneficiary supplied by the caller.

### Recommendation
Do not mark the reward as paid/removed until remote execution is confirmed. Options:
- Use a paid, receipted XCM pattern (e.g., request a `QueryResponse`/notification of successful execution from AssetHub) before removing `RelayerRewards`, or
- Keep the reward entry in a "pending" state until confirmation, only clearing it on a positive execution acknowledgement, and re-crediting it automatically if a trap/failure notification is received, or
- At minimum, validate the beneficiary `Location` more strictly before consuming the local reward record, and add a recovery/claim path for `AssetsTrapped` funds tied back to the relayer's registered reward.

### Proof of Concept
1. A relayer earns a Snowbridge reward, tracked in `RelayerRewards` on BridgeHub.
2. Relayer calls `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation(bad_location)` where `bad_location` is a `Location` that AssetHub's XCM barrier/converter cannot resolve to a valid depositable account (e.g. malformed junctions, or an account below existential deposit under `AllowUnpaidExecutionFrom`).
3. On BridgeHub: `do_claim_rewards` → `PayAccountOnLocation::pay_reward` builds and sends the XCM; `validate_send`, `charge_fees`, and `deliver` all succeed since these only check local dispatch/routing, not remote deposit outcome — see: [4](#0-3) 
4. `pay_reward` returns `Ok(())`; `RelayerRewards` entry is removed and `Event::RewardPaid` is emitted on BridgeHub.
5. On AssetHub, the `DepositAsset` instruction fails to complete (e.g., account doesn't exist, or barrier rejects), and the reserve-minted asset is trapped (`pallet_xcm::Event::AssetsTrapped`) instead of being credited to the relayer.
6. Net effect: BridgeHub records the reward as fully and successfully paid, but the relayer's balance on AssetHub is unchanged — the reward value is lost/trapped with no automatic recovery path tied to the original claim.

### Citations

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

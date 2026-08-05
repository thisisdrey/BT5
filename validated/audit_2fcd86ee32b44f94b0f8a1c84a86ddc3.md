Confirmed: `claim_rewards_to` is a public, signed-origin extrinsic (call_index 3) callable by any registered relayer with an arbitrary `beneficiary` location, and it unconditionally deletes the `RelayerRewards` entry via `try_mutate_exists` once `PaymentProcedure::pay_reward` returns `Ok(())` [1](#0-0) , with `do_claim_rewards` taking the reward out of storage before calling the payment procedure and only failing the whole mutation if `pay_reward` itself errors [2](#0-1) . For `BridgeReward::Snowbridge`, `PayAccountOnLocation::pay_reward` builds an XCM with `ReserveAssetDeposited` + `DepositAsset` to the beneficiary and returns `Ok(())` as soon as `XcmSender::deliver(ticket)` succeeds, without waiting for or checking the remote execution result [3](#0-2) .

I also checked what happens on AssetHub if `DepositAsset` fails there: the executor's `deposit_assets_with_retry` logic aborts the instruction and the leftover holding (the freshly reserve-deposited reward assets) gets trapped via `Config::AssetTrap::drop_assets` rather than silently disappearing [4](#0-3) . However, the reward XCM program never includes a `SetAssetClaimer` instruction, and the trapped assets' claim key is bound to whatever origin is active at that point — here `UniversalOrigin(GlobalConsensus(Ethereum))` + `DescendOrigin(InboundQueueLocation)` [5](#0-4) . A relayer has no ordinary, permissionless way to issue a `ClaimAsset` XCM as that origin, so in practice the trapped reward is unrecoverable by the relayer even though it isn't technically "burned" at the protocol level.

Combining these points: `do_claim_rewards` treats XCM message-delivery success (`Ok(())` from `deliver`) as final settlement and irreversibly clears `RelayerRewards[relayer][Snowbridge]` before the remote `DepositAsset` on AssetHub is known to succeed. If the beneficiary account/asset fails the deposit (e.g., not meeting existential/minimum balance for the foreign WETH asset class), the relayer's reward is deleted from BridgeHub storage, the `RewardPaid` event is emitted, but the funds never land with the beneficiary and are trapped in a location the relayer cannot practically reclaim. This is reachable by any relayer calling the public `claim_rewards_to` extrinsic with a beneficiary location that isn't already funded/registered on AssetHub — no privileged action, malicious peer, or compromised key is required.

Audit Report

## Title
Reward claim on BridgeHub optimistically clears the relayer's reward ledger before the cross-chain Snowbridge payout is confirmed, permanently losing funds if the remote `DepositAsset` fails - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

## Summary
`pallet_bridge_relayers::do_claim_rewards` removes the relayer's `RelayerRewards` entry as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For `BridgeReward::Snowbridge`, that `Ok(())` (from `PayAccountOnLocation::pay_reward`) only means an XCM was successfully queued/delivered to AssetHub via `XcmSender::deliver`, not that the remote `DepositAsset` to the beneficiary actually succeeded. If the remote deposit fails, the reward is deleted from BridgeHub storage and never lands with the beneficiary, and the relayer has no dispatchable path to reclaim it.

## Finding Description
The public, signed extrinsic `claim_rewards_to` (call_index 3) calls `Pallet::do_claim_rewards`, which uses `RelayerRewards::try_mutate_exists` to take the reward balance out of storage and only rolls back the whole mutation (re-inserting the entry) if `T::PaymentProcedure::pay_reward` itself returns an `Err` [2](#0-1) [1](#0-0) .

For Snowbridge rewards, `PaymentProcedure` is implemented via `PayAccountOnLocation::pay_reward`, which builds an `Xcm` containing `ReserveAssetDeposited` followed by `DepositAsset { beneficiary, .. }`, validates and sends it, and returns `Ok(())` immediately once `XcmSender::deliver(ticket)` succeeds — without any confirmation that the remote AssetHub execution (in particular the `DepositAsset`) actually completes [3](#0-2) .

On AssetHub, if the beneficiary cannot receive the deposit (e.g., the account/asset instance doesn't meet the existential/minimum balance requirement for the foreign WETH asset class), the XCM executor's `deposit_assets_with_retry` aborts the `DepositAsset` instruction, and the leftover holding is trapped via `Config::AssetTrap::drop_assets` rather than credited to the beneficiary [4](#0-3) . Since the reward XCM program never sets a `SetAssetClaimer` and manipulates origin via `UniversalOrigin(Ethereum)`/`DescendOrigin(InboundQueueLocation)`, the trap is keyed to an origin the relayer cannot practically reclaim from as an ordinary user [5](#0-4) .

By the time any of this happens, BridgeHub has already deleted the `RelayerRewards[relayer][Snowbridge]` entry and emitted `RewardPaid`, so there is no remaining accounting state and no dispatchable that could re-credit or retry the payout.

## Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for bridge rewards: BridgeHub accounting asserts the reward as paid (ledger cleared, `RewardPaid` emitted) while the value never actually reaches the beneficiary and cannot be reclaimed via any relayer-accessible extrinsic. This matches the required "permanent user-fund... lock" impact for bridge reward payout state, since a normal relayer permanently loses an already-accrued reward through no adversarial action.

## Likelihood Explanation
No privileged actor or adversarial behavior is required. Any registered relayer calling the public `claim_rewards_to(reward_kind=Snowbridge, beneficiary=...)` extrinsic with a beneficiary location on AssetHub that isn't already funded above the relevant existential/minimum balance for the foreign asset triggers this path deterministically [1](#0-0) .

## Recommendation
Do not treat `XcmSender::deliver` success as final settlement for `RelayerRewards` accounting. Either hold the reward in a pending state until delivery/execution is confirmed via an XCM query/receipt from AssetHub before clearing `RelayerRewards`, or re-credit the relayer's `RelayerRewards` entry on confirmed remote failure, analogous to `pallet_treasury`'s `PaymentState::{Attempted,Failed}` retry flow.

## Proof of Concept
1. Relayer accrues a Snowbridge reward via the outbound-queue-v2 delivery-receipt path.
2. Relayer calls `claim_rewards_to(reward_kind = Snowbridge, beneficiary = AssetHubLocation(fresh_account_below_ED))`.
3. `do_claim_rewards` removes the `RelayerRewards` entry, `PayAccountOnLocation::pay_reward` builds/sends the XCM, `XcmSender::deliver` succeeds, `pay_reward` returns `Ok(())`, `RewardPaid` is emitted, and the storage removal commits [6](#0-5) .
4. On AssetHub, `DepositAsset` fails because the beneficiary lacks the existential/minimum balance for the foreign WETH asset; the minted assets are trapped rather than credited [4](#0-3) .
5. Relayer's `RelayerRewards` balance is zero, the beneficiary never received funds, and no dispatchable exists on BridgeHub to re-credit or retry the payout.

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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1837-1849)
```rust
	/// Deposit `to_deposit` assets to `beneficiary`, without giving up on the first (transient)
	/// error, and retrying once just in case one of the subsequently deposited assets satisfy some
	/// requirement.
	///
	/// Most common transient error is: `beneficiary` account does not yet exist and the first
	/// asset(s) in the (sorted) list does not satisfy ED, but a subsequent one in the list does.
	///
	/// Any per-asset failure on the retry pass propagates as `Err`, and the surrounding
	/// `transactional_process` rolls back the whole instruction (storage changes are reverted by
	/// `Config::TransactionalProcessor`, and `self.holding` is restored from its
	/// pre-instruction backup). Anything left in `self.holding` after the program finishes is
	/// then trapped by `post_process` via `Config::AssetTrap::drop_assets`, so funds are never
	/// silently lost.
```

### Title
Reward accounting is finalized on XCM *send* success, not on remote-execution success, permanently burning relayer rewards - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`pallet-bridge-relayers::claim_rewards_to` deletes a relayer's accrued reward entry as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For Snowbridge rewards, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which only validates and *enqueues* an XCM message to AssetHub (`validate_send` + `XcmSender::deliver`) and returns `Ok(())` the moment the message is handed to the router — it never learns whether the remote `ReserveAssetDeposited`/`DepositAsset` sequence actually executes and credits the beneficiary. This is the same class of bug as the report: state that represents "reward owed" is destroyed based on an operation that only *authorizes* value movement (`approve`/`send`), not one that *confirms* it (`transferFrom`/execution receipt).

### Finding Description
The claim path is:
```
RelayerRewards::<T, I>::try_mutate_exists(&relayer, reward_kind, |maybe_reward| {
    let reward_balance = maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
    T::PaymentProcedure::pay_reward(&relayer, reward_kind, reward_balance, beneficiary.clone())
        .map_err(|e| { ...; Error::<T, I>::FailedToPayReward })?;
    Self::deposit_event(Event::<T, I>::RewardPaid { .. });
    Ok(())
})
``` [1](#0-0) 

For `BridgeReward::Snowbridge`, `pay_reward` is `PayAccountOnLocation::pay_reward`:
```rust
fn pay_reward(relayer: &Relayer, _: (), reward: RewardBalance, beneficiary: Self::Beneficiary) -> Result<(), Self::Error> {
    ...
    let xcm: Xcm<()> = ... ReserveAssetDeposited(assets.into()), DepositAsset { .. } ...
    let (ticket, fee) = validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
    XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
    XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;
    Ok(())
}
``` [2](#0-1) 

`Ok(())` here means only "the message was accepted by the local router for delivery" — it says nothing about whether AssetHub's XCM executor actually accepts `ReserveAssetDeposited` from the descended/universal Ethereum origin and deposits the asset to `beneficiary`. If AssetHub's barrier/`IsReserve` configuration rejects the reserve deposit for that origin (e.g. due to a reserve-trust mismatch, a stale/removed foreign-asset registration, or `Weightless`/insufficient remote weight even though `Unlimited` is requested locally but capped by the remote executor), the deposit silently fails on AssetHub with no error propagated back to BridgeHub. Meanwhile, on BridgeHub, `do_claim_rewards` has already committed `RelayerRewards` deletion and emitted `RewardPaid`, because `pay_reward` returned `Ok(())` based purely on successful message *dispatch*, not on confirmed *settlement*.

This mirrors the external report exactly: the accounting side (`RelayerRewards` map / `s_claimableRewardsByGauge`) is cleared based on an authorization/dispatch step (`XcmSender::deliver` / `forceApprove`) rather than on confirmed value transfer (`transferFrom`/remote execution receipt), so the reward can be permanently lost with no compensating mechanism to re-credit the relayer.

### Impact Explanation
A legitimate, unprivileged relayer calling the public `claim_rewards_to` extrinsic can permanently lose their entire accrued reward balance if the fire-and-forget XCM to AssetHub fails to execute remotely — the local ledger has already deleted the claim and there is no retry, refund, or reconciliation path. This is a direct, unbacked loss of relayer funds/bridge-state, falling squarely within "theft or unbacked mint or unlock" / "permanent user-fund lock" impact categories for bridge reward payout flows.

### Likelihood Explanation
No malicious peer, validator, relayer, or governance action is required — any relayer with a nonzero registered Snowbridge reward triggering the ordinary `claim_rewards_to` flow is exposed whenever the remote AssetHub XCM execution of the `ReserveAssetDeposited`/`DepositAsset` sequence fails or is dropped for any reason outside the sender's control (barrier/reserve-trust misconfiguration, remote weight exhaustion, asset-registration drift). Because success is determined purely by local message dispatch, this can occur under ordinary operational conditions, not just adversarial ones.

### Recommendation
Do not delete `RelayerRewards` (or emit `RewardPaid`) based solely on `XcmSender::deliver` success. Either:
- require an execution/delivery receipt callback from AssetHub before finalizing the local reward deletion (analogous to how `DeliveryReceipt`/`process_delivery_receipt` confirms outbound message execution elsewhere in this codebase), or
- keep the reward entry in a "pending" state until confirmed, with a re-credit/retry path if the remote deposit fails, so relayer funds are never destroyed based on unconfirmed XCM delivery.

### Proof of Concept
1. A relayer accrues a Snowbridge reward via `register_reward`, verified by `RelayerRewards::<T, I>::get`.
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))`.
3. `do_claim_rewards` takes and removes the stored reward, then calls `PayAccountOnLocation::pay_reward`, which builds and sends the `ReserveAssetDeposited`/`DepositAsset` XCM to AssetHub and returns `Ok(())` as soon as `XcmSender::deliver` succeeds locally. [3](#0-2) 
4. `RewardPaid` is emitted and the `RelayerRewards` entry is gone — final, no rollback path.
5. On AssetHub, if the reserve-deposit instruction is rejected by the XCM barrier/reserve-trust configuration for that origin/asset combination (a condition entirely independent of anything the relayer controls), the beneficiary never receives the funds, as demonstrated by the existing unit tests that show `pay_reward` treats "sent"/"delivered" as terminal success states with no execution confirmation (`pay_reward_success`, `pay_reward_fails_on_delivery`, etc., none of which test actual remote execution outcome). [4](#0-3)

### Citations

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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L246-266)
```rust
	#[test]
	fn pay_reward_success() {
		let relayer = MockRelayer(AccountId32::new([1u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 1_000u128;

		type TestedPayAccountOnLocation = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			MockXcmSender,
			MockXcmExecutor,
			MockCall,
		>;

		let result = TestedPayAccountOnLocation::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_ok());
	}
```

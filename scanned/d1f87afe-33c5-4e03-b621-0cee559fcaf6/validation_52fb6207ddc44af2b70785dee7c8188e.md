## Finding: Unverified cross-chain settlement in Snowbridge relayer reward payout — analogous to the `delegateTreasury` unverified-transfer bug

The DEXE bug's core pattern — mutating internal accounting state to reflect a transfer as "done" without verifying the transfer actually completed — has a direct analog in the Snowbridge relayer reward payout path.

### Title
Relayer reward is erased from `RelayerRewards` ledger before the cross-chain XCM deposit is confirmed to execute, allowing permanent reward loss on destination execution failure - (File: `bridges/snowbridge/primitives/core/src/reward.rs`, `bridges/modules/relayers/src/lib.rs`)

### Summary
`pallet_bridge_relayers::do_claim_rewards` removes the relayer's reward entry from storage and emits `RewardPaid` as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge reward kind, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which only confirms that an `UnpaidExecution` XCM was accepted by the local router (`XcmSender::deliver`) — it never confirms that the `ReserveAssetDeposited` + `DepositAsset` instructions actually executed successfully on the remote chain (AssetHub). This mirrors the DEXE `delegateTreasury` flaw: state is advanced to reflect a completed transfer based only on the "send" step succeeding, not on the destination-side settlement succeeding.

### Finding Description
`do_claim_rewards` in [1](#0-0)  pops the reward out of `RelayerRewards` via `maybe_reward.take()`, calls `T::PaymentProcedure::pay_reward(...)`, and if that call returns `Ok(())`, permanently commits the removal and emits `Event::RewardPaid`.

For the Snowbridge reward kind, `pay_reward` is `PayAccountOnLocation::pay_reward` at [2](#0-1) :
```
let (ticket, fee) = validate_send::<XcmSender>(AssetHubLocation::get(), xcm)...
XcmExecutor::charge_fees(relayer.clone(), fee)...
XcmSender::deliver(ticket)...
Ok(())
```
This function returns `Ok(())` the moment the XCM message is handed off to the outbound queue/router (`XcmSender::deliver`). It provides zero feedback about whether the destination chain (AssetHub) actually executes `ReserveAssetDeposited`/`DepositAsset` and credits the beneficiary. The instructions use `UnpaidExecution { weight_limit: Unlimited, .. }`, meaning execution outcome on AssetHub depends entirely on AssetHub-side conditions (its `IsReserve` filter accepting the spoofed Ethereum-origin universal origin, the beneficiary location resolving to a valid account, asset registration, existential deposit, weight availability in the message queue, etc.) — none of which are checked or reported back to BridgeHub.

Because `do_claim_rewards` already deleted the ledger entry and emitted `RewardPaid` before any of that destination-side execution occurs, any failure on the AssetHub side (bad beneficiary conversion, message-queue processing failure, asset not yet registered, XCM version mismatch, `DepositAsset`'s `AllCounted(1)` mismatch, etc.) results in silent, permanent loss of the relayer's reward — the accounting entry is gone on BridgeHub, and the funds were never actually credited on AssetHub.

This is exactly the invariant break described in the external report: the code assumes "successful hand-off == successful transfer" and advances authoritative state on that assumption, rather than "trust but verify."

### Impact Explanation
This falls under "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and "permanent user-fund ... lock." An honest, unprivileged relayer who has earned a legitimate reward can have it permanently and silently erased with no recovery path, purely due to a downstream execution failure that the payout code does not detect or roll back for. No malicious peer, validator, governance actor, or admin is required — it's a plain logic gap in the settlement pipeline of a live-scope (BridgeHub/Snowbridge) pallet.

### Likelihood Explanation
The `UnpaidExecution` + cross-chain `DepositAsset` path is exercised on every `claim_rewards_to` call for Snowbridge rewards (this is the *only* supported payout path for `BridgeReward::Snowbridge`, per `BridgeRewardPayer::pay_reward`, see [3](#0-2) ). Any of the numerous destination-side failure modes (weight starvation of the message queue, asset-registration edge cases, beneficiary location edge cases) suffices to trigger loss — no attacker action is even needed, just an unlucky/edge-case execution on AssetHub.

### Recommendation
Do not remove the `RelayerRewards` entry (or emit `RewardPaid`) until destination-side execution success is confirmed. This requires either: (a) using a confirmable transfer mechanism with an XCM query/callback (`ReportOutcome`/`SetAppendix` + `on_response`) that only finalizes the ledger removal upon a success acknowledgment from AssetHub, or (b) keeping a "pending" state for the reward and only clearing it upon confirmed receipt, with a retry/reclaim path if the remote execution fails.

### Proof of Concept
1. A relayer accrues a Snowbridge reward via `RewardLedger::register_reward` on BridgeHub.
2. Relayer calls `claim_rewards_to` with a `BridgeRewardBeneficiaries::AssetHubLocation` beneficiary whose location, once XCM-executed on AssetHub, fails to resolve to a valid/fundable account (e.g., malformed junction, or an account below existential deposit for `ForeignAssets`) — or simply have the outbound HRMP/XCMP message queue on AssetHub run out of weight/fail decode for any edge-case reason.
3. On BridgeHub: `PayAccountOnLocation::pay_reward` returns `Ok(())` because `XcmSender::deliver` succeeded in queuing the message (see the passing `pay_reward_success`-style test flow at [4](#0-3) , which asserts success purely on delivery, not on remote execution).
4. `do_claim_rewards` then commits `maybe_reward.take()` and emits `RewardPaid`.
5. On AssetHub, execution fails (per step 2); `ForeignAssets::Deposited` event never fires, and no funds are credited to the beneficiary.
6. Relayer permanently lost the reward — nothing left in `RelayerRewards`, nothing credited on AssetHub, and no automatic mechanism restores it.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
```

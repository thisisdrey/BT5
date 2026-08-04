## Analysis

The external report's core invariant: **a caller treats a value-transfer call's return as unconditional success, without verifying the transfer actually landed at the beneficiary** (USDT-style silent failure). The strongest local analog to this pattern is Snowbridge's relayer reward payout path, where `pallet-bridge-relayers` finalizes/clears a reward as "paid" the moment an XCM message is *sent*, not when the beneficiary account on AssetHub actually *receives* the funds.

### Title
Reward claim settles and clears the ledger before the cross-chain `DepositAsset` actually executes, permanently burning relayer funds on remote-execution failure - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PayAccountOnLocation::pay_reward` (the `bp_relayers::PaymentProcedure` implementation used for `BridgeReward::Snowbridge` in `BridgeRewardPayer::pay_reward`) returns `Ok(())` as soon as the reward-minting XCM is successfully *validated and delivered* to the outbound queue — it does not, and structurally cannot, wait for or confirm that the remote `DepositAsset` instruction actually executes on AssetHub. [1](#0-0) 

### Finding Description
`pay_reward` builds an XCM (`UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset`) targeting AssetHub, validates it, charges local delivery fees, and calls `XcmSender::deliver(ticket)`, returning `Ok(())` immediately after delivery succeeds: [2](#0-1) 

This `Ok(())` is consumed by `pallet_bridge_relayers`'s `claim_rewards`/`claim_rewards_to` extrinsics via the `BridgeRewardPayer::pay_reward` dispatch for `BridgeReward::Snowbridge`: [3](#0-2) 

Because `PaymentProcedure::pay_reward` returning `Ok(())` is the pallet's sole success signal, the reward record is treated as settled on BridgeHub the moment the message is *enqueued for delivery*, not when the `DepositAsset` on AssetHub actually completes. If the remote XCM execution fails (e.g., `DepositAsset` trapped because the beneficiary account cannot be created, insufficient existential deposit, asset registration issues, or a Barrier/filter rejection on AssetHub), the funds are never delivered, yet the relayer's claim has already been consumed/cleared on BridgeHub with no compensating mechanism to re-credit or retry. This is structurally identical to trusting a token's `transfer()` return value without confirming the actual balance movement — except here the "return value" is merely "the XCM was accepted into the queue," not "value was received by the beneficiary."

This exact bug class has already manifested once in this codebase and been patched: `prdoc/stable2509/pr_9746.prdoc` documents "Snowbridge Inbound Queue V2 relayer tip payout fix — Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been burnt," confirming that unconfirmed-settlement assumptions in this reward/tip payment logic have previously caused permanent fund loss in this exact bridge. [4](#0-3) 

### Impact Explanation
A relayer's earned reward is permanently lost (burnt) if the remote-chain leg of the reward-payment XCM fails after the local ledger entry has already been cleared. This is a "permanent user-fund lock/loss" impact under the accepted scope: value is deducted/committed on one chain based on an unconfirmed cross-chain execution assumption, with no atomic all-or-nothing settlement between the two legs.

### Likelihood Explanation
Any unprivileged relayer can trigger `claim_rewards_to` for a `BridgeReward::Snowbridge` reward with an `AssetHubLocation` beneficiary that is crafted (or happens) to fail `DepositAsset` execution on AssetHub — e.g., a beneficiary account with no existing entry and reward amount below the AssetHub existential deposit for the reward's foreign asset, or a beneficiary location that XCM execution otherwise rejects. No privileged actor, governance, or malicious peer/validator is required; the caller is the relayer itself acting on its own claim, which matches an unprivileged public-entrypoint path.

### Recommendation
Do not treat XCM delivery as final settlement for reward accounting. Either (a) keep the reward pending until a confirmed execution receipt/ack from AssetHub is received before clearing it from the ledger, or (b) make the local debit reversible/re-creditable if a corresponding failure notification (e.g., via `ExpectTransactStatus`/response instructions or a settlement callback) indicates the remote `DepositAsset` did not succeed, analogous to using `SafeERC20`-style guarantees instead of assuming success from a non-reverting call.

### Proof of Concept
1. Relayer accumulates a `BridgeReward::Snowbridge` reward via `register_reward` (from prior relaying activity). [5](#0-4) 
2. Relayer calls `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation(...)` pointing at an account/location for which the eventual `DepositAsset` on AssetHub will fail (e.g., an amount below ED for the WETH-derived asset, or a beneficiary account that cannot be created). [6](#0-5) 
3. `PayAccountOnLocation::pay_reward` validates and delivers the XCM successfully and returns `Ok(())`, so `pallet_bridge_relayers` clears the relayer's reward entry on BridgeHub. [2](#0-1) 
4. On AssetHub, the `DepositAsset` instruction traps/fails, so no asset is ever credited to the beneficiary — the reward is permanently lost, with the BridgeHub side already showing `RewardPaid`.

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

**File:** prdoc/stable2509/pr_9746.prdoc (L1-13)
```text
title: Snowbridge Inbound Queue V2 relayer tip payout fix

doc:
- audience: Runtime Dev
  description: |
    Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been
    burnt.

crates:
- name: snowbridge-pallet-inbound-queue-v2
  bump: patch
- name: snowbridge-test-utils
  bump: minor
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L45-49)
```rust
		BridgeRelayers::register_reward(
			(&relayer_account.clone()).into(),
			BridgeReward::Snowbridge,
			reward_amount,
		);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L69-74)
```rust
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_ok!(result);
```

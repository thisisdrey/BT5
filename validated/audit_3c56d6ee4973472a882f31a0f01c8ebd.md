## Analysis

The external report's core broken invariant: a recipient-status check (blacklist) that exists only on the **destination** chain, invisible to the **source** chain when it locks/emits value, causes the credit step to fail — and instead of routing the value to a recoverable fallback, the transaction reverts, permanently stranding funds that were already committed on the source side.

The closest verified local analog is in Snowbridge's inbound message conversion, where a permissionless Ethereum-side call can name *any* AssetHub beneficiary, but AssetHub's own recipient-status rules (`pallet-assets` `Blocked`/frozen accounts) are enforced only at `DepositAsset` execution time on the destination chain, with no fallback routing for the direct-AssetHub delivery branch.

### Title
Snowbridge inbound token transfer traps bridged assets with no recoverable fallback when destination beneficiary is Blocked/Frozen on AssetHub - (File: `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`)

### Summary
`MessageToXcm::convert_send_token` builds the XCM program that credits a bridged ERC-20/Ether transfer to an AssetHub beneficiary supplied by the permissionless Ethereum `Gateway` contract call. For the case where the beneficiary is on AssetHub itself, the instruction list ends with a single `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }` and includes **no** `SetAppendix`/fallback deposit to a recoverable account, unlike the sibling-parachain branch which does append a fallback deposit to the bridge sovereign. [1](#0-0) 

### Finding Description
Assets are reserve-deposited on BridgeHub for the value locked on Ethereum, then the XCM is routed to AssetHub to execute `DepositAsset` into the attacker-chosen beneficiary. [2](#0-1) 

If that beneficiary account is `Blocked` (or frozen) in `pallet-assets`/`pallet-balances` on AssetHub — a state that is entirely local to the destination and unknown to the Ethereum-side sender at submission time — the deposit fails with `TokenError::Blocked`/`Frozen`. [3](#0-2) [4](#0-3) 

When the final `DepositAsset` instruction errors, the XCM executor's `post_process` does not revert the whole program's earlier `ReserveAssetDeposited`/`WithdrawAsset` effects (XCM instructions are not atomic per-message); instead any assets still sitting in the holding register are handed to `AssetTrap::drop_assets`, keyed by `asset_claimer` hint or `context.origin`. [5](#0-4) 

The pallet-message-queue processor (`ProcessXcmMessage`) treats both `Outcome::Incomplete` and this failure path as an XCM-executor-level result that is turned into `Ok(false)`/dropped, not a retry signal. [6](#0-5) 
And `pallet-message-queue`'s `process_message_payload` marks any `Ok(_)` result — success `true` or `false` — as `Processed`, i.e. the message is permanently consumed and never retried, regardless of whether the deposit actually succeeded. [7](#0-6) 
This exact "silently accepted, funds not delivered" pattern is already reproduced in the repo's own emulated test, which asserts `Event::Processed { success: false, .. }` for a comparable failed deposit scenario. [8](#0-7) 

This is the same broken invariant as the `StakedUSDeOFT` report: the source chain (Ethereum Gateway, fully permissionless) has no visibility into destination-chain recipient-blocking state, and there is no redirect-to-fallback-account logic (as `StakedUSDeOFTAdapter._credit` implements by crediting `owner()` instead of reverting) for the direct AssetHub-beneficiary branch of `convert_send_token`.

### Impact Explanation
Value that was already locked/burned as collateral on Ethereum is emitted as an unretriable, permanently-dropped message once it reaches the blocked-beneficiary case on AssetHub, with recovery possible only through the generic XCM `AssetTrap`/`ClaimAsset` mechanism — which requires knowledge of the exact origin/claimer location and a manual `claim_assets` extrinsic, not something the blocked beneficiary or an ordinary relayer can straightforwardly execute. This matches the "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" gate categories: bridged value is accepted as settled by the message queue (`Processed`) while never reaching the intended beneficiary and lacking the same automatic-fallback protection Snowbridge's own sibling-parachain code path provides.

### Likelihood Explanation
Triggering requires only that a legitimate AssetHub compliance/freeze action (`pallet-assets::block`, or a balance freeze) has been applied to an account — an ordinary, expected administrative state, not privileged abuse as the root cause of the bug. Any user (attacker or otherwise) can then submit — or a relayer will submit on their behalf — a Gateway `sendToken` call targeting that already-blocked AccountId32 beneficiary; no relayer/validator/governance collusion is required, since the destination status is public on-chain state that anyone can check before triggering the transfer.

### Recommendation
Mirror the `StakedUSDeOFTAdapter` fix pattern: in `MessageToXcm::convert_send_token` (and the v2 `MessageToXcm::convert` equivalent), add a `SetAppendix`/fallback `DepositAsset` to a recoverable, bridge-controlled sovereign/owner account for the local-AssetHub-beneficiary branch, matching the fallback already present for the sibling-parachain branch, so that a blocked/frozen beneficiary cannot cause bridged value to become effectively unrecoverable.

### Proof of Concept
1. On AssetHub, an asset administrator calls `Assets::block(origin, asset_id, target)` on account `X` (ordinary compliance action, not attacker-controlled).
2. Anyone calls the Ethereum `Gateway` contract's `sendToken`/equivalent naming beneficiary `X` (AccountId32) as recipient, locking ERC-20/Ether collateral on Ethereum.
3. Snowbridge relayer submits the proof; `MessageToXcm::convert_send_token` builds `[..., DepositAsset { assets: Wild(AllCounted(2)), beneficiary: X }]` with no fallback appendix for the local-AssetHub path. [1](#0-0) 
4. On AssetHub, `DepositAsset` fails with `TokenError::Blocked`; `XcmExecutor::post_process` traps the held assets via `AssetTrap::drop_assets`. [9](#0-8) 
5. `pallet-message-queue` marks the message `Processed` (dropped, non-retryable) regardless of the `false` success flag. [7](#0-6) 
6. Collateral remains locked/burned on Ethereum while the corresponding AssetHub-side value is trapped and not delivered to `X`, with no automatic redirect to a recoverable account.

### Citations

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L335-382)
```rust
		let mut instructions = vec![
			ReceiveTeleportedAsset(total_fee_asset.into()),
			BuyExecution { fees: asset_hub_fee_asset, weight_limit: Unlimited },
			DescendOrigin(PalletInstance(inbound_queue_pallet_index).into()),
			UniversalOrigin(GlobalConsensus(network)),
			ReserveAssetDeposited(asset.clone().into()),
			ClearOrigin,
		];

		match dest_para_id {
			Some(dest_para_id) => {
				let dest_para_fee_asset: Asset = (Location::parent(), dest_para_fee).into();
				let bridge_location = Location::new(2, GlobalConsensus(network));

				instructions.extend(vec![
					// After program finishes deposit any leftover assets to the snowbridge
					// sovereign.
					SetAppendix(Xcm(vec![DepositAsset {
						assets: Wild(AllCounted(2)),
						beneficiary: bridge_location,
					}])),
					// Perform a deposit reserve to send to destination chain.
					DepositReserveAsset {
						// Send over assets and unspent fees, XCM delivery fee will be charged from
						// here.
						assets: Wild(AllCounted(2)),
						dest: Location::new(1, [Parachain(dest_para_id)]),
						xcm: vec![
							// Buy execution on target.
							BuyExecution { fees: dest_para_fee_asset, weight_limit: Unlimited },
							// Deposit assets to beneficiary.
							DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
							// Forward message id to destination parachain.
							SetTopic(message_id.into()),
						]
						.into(),
					},
				]);
			},
			None => {
				instructions.extend(vec![
					// Deposit both asset and fees to beneficiary so the fees will not get
					// trapped. Another benefit is when fees left more than ED on AssetHub could be
					// used to create the beneficiary account in case it does not exist.
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
				]);
			},
		}
```

**File:** substrate/frame/assets/src/types.rs (L154-173)
```rust
/// The status of an asset account.
#[derive(Clone, Encode, Decode, Eq, PartialEq, Debug, MaxEncodedLen, TypeInfo)]
pub enum AccountStatus {
	/// Asset account can receive and transfer the assets.
	Liquid,
	/// Asset account cannot transfer the assets.
	Frozen,
	/// Asset account cannot receive and transfer the assets.
	Blocked,
}
impl AccountStatus {
	/// Returns `true` if frozen or blocked.
	pub fn is_frozen(&self) -> bool {
		matches!(self, AccountStatus::Frozen | AccountStatus::Blocked)
	}
	/// Returns `true` if blocked.
	pub fn is_blocked(&self) -> bool {
		matches!(self, AccountStatus::Blocked)
	}
}
```

**File:** substrate/frame/support/src/traits/tokens/misc.rs (L129-150)
```rust
/// One of a number of consequences of withdrawing a fungible from an account.
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub enum DepositConsequence {
	/// Deposit couldn't happen due to the amount being too low. This is usually because the
	/// account doesn't yet exist and the deposit wouldn't bring it to at least the minimum needed
	/// for existence.
	BelowMinimum,
	/// Deposit cannot happen since the account cannot be created (usually because it's a consumer
	/// and there exists no provider reference).
	CannotCreate,
	/// The asset is unknown. Usually because an `AssetId` has been presented which doesn't exist
	/// on the system.
	UnknownAsset,
	/// An overflow would occur. This is practically unexpected, but could happen in test systems
	/// with extremely small balance types or balances that approach the max value of the balance
	/// type.
	Overflow,
	/// Account continued in existence.
	Success,
	/// Account cannot receive the assets.
	Blocked,
}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L397-441)
```rust
	/// This includes refunding surplus weight, trapping extra holding funds, and returning any
	/// errors during execution.
	pub fn post_process(mut self, xcm_weight: Weight) -> Outcome {
		// We silently drop any error from our attempt to refund the surplus as it's a charitable
		// thing so best-effort is all we will do.
		let _ = self.refund_surplus();
		drop(self.trader);

		let mut weight_used = xcm_weight.saturating_sub(self.total_surplus);

		if !self.holding.is_empty() {
			tracing::trace!(
				target: "xcm::post_process",
				holding_register = ?self.holding,
				context = ?self.context,
				original_origin = ?self.original_origin,
				"Trapping assets in holding register",
			);
			let claimer = self
				.asset_claimer
				.as_ref()
				.or(self.context.origin.as_ref())
				.unwrap_or(&self.original_origin);
			let trap_weight = Config::AssetTrap::drop_assets(claimer, self.holding, &self.context);
			weight_used.saturating_accrue(trap_weight);
		};

		match self.error {
			None => Outcome::Complete { used: weight_used },
			// TODO: #2841 #REALWEIGHT We should deduct the cost of any instructions following
			// the error which didn't end up being executed.
			Some((index, error)) => {
				tracing::trace!(
					target: "xcm::post_process",
					instruction = ?index,
					?error,
					original_origin = ?self.original_origin,
					"Execution failed",
				);
				Outcome::Incomplete {
					used: weight_used,
					error: InstructionError { index: index.try_into().unwrap_or(u8::MAX), error },
				}
			},
		}
```

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L91-127)
```rust
		let (consumed, result) = match XcmExecutor::execute(origin.into(), pre, id, Weight::zero())
		{
			Outcome::Complete { used } => {
				tracing::trace!(
					target: LOG_TARGET,
					"XCM message execution complete, used weight: {used}",
				);
				(used, Ok(true))
			},
			Outcome::Incomplete { used, error: InstructionError { index, error } } => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					?used,
					"XCM message execution incomplete",
				);
				(used, Ok(false))
			},
			// In the error-case we assume the worst case and consume all possible weight.
			Outcome::Error(InstructionError { error, index }) => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					"XCM message execution error",
				);
				let error = match error {
					xcm::latest::Error::ExceedsStackLimit => ProcessMessageError::StackLimitReached,
					_ => ProcessMessageError::Unsupported,
				};

				(required, Err(error))
			},
		};
		meter.consume(consumed);
		result
```

**File:** substrate/frame/message-queue/src/lib.rs (L1618-1628)
```rust
			Ok(success) => {
				// Success
				let weight_used = meter.consumed().saturating_sub(prev_consumed);
				Self::deposit_event(Event::<T>::Processed {
					id: id.into(),
					origin,
					weight_used,
					success,
				});
				MessageExecutionStatus::Processed
			},
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_edge_case.rs (L86-92)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::MessageQueue(pallet_message_queue::Event::Processed{ success:false, .. }) => {},]
		);
	});
```

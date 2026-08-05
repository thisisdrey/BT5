### Title
Accumulate-and-forward pallet permanently burns treasury/fee revenue on local success while destination-side deposit failure traps it with no recovery - ([File: polkadot/xcm/xcm-builder/src/forwarder.rs])

### Summary
`pallet_accumulate_and_forward` (used by Westend relay chain and the collectives/coretime/people/bridge-hub system-chain runtimes) periodically teleports accumulated fee/dust/coretime revenue off-chain via `TeleportForwarderForAccountId32::forward`. The pallet treats "local XCM execution succeeded" as final success and burns the funds locally / marks the transfer as done, even though the remote `DepositAsset` on the destination chain can independently fail, permanently trapping the teleported value with, by the code's own documentation, "no automatic recovery path."

### Finding Description
The flow mirrors the ZetaChain bug class exactly: value is destroyed on the source chain based on an optimistic assumption that the paired mint/deposit on the other chain will succeed, and if it doesn't, the funds are gone.

`Pallet::on_idle` reads the accumulation account's reducible balance and calls `T::Forwarder::forward(accumulation_account, available_funds)`, treating an `Ok(())` result as final settlement (deposits `ForwardSucceeded`); there is no follow-up check and no retry path once `Ok` is returned: [1](#0-0) 

`TeleportForwarderForAccountId32::forward` builds an XCM that withdraws the native asset locally, and uses `InitiateTransfer` with `AssetTransferFilter::Teleport` to move it to `Dest`, with a `remote_xcm` that ends in `DepositAsset { assets: Wild(AllCounted(1)), beneficiary }`. It wraps only the **local** `prepare_and_execute` call in `with_transaction`, and returns `Ok(())` as soon as the local outcome is `Outcome::Complete` — i.e. as soon as the local burn happened and the message was successfully queued to the router: [2](#0-1) 

The module doc explicitly states the resulting invariant break: local-execution failures roll back cleanly, but "[o]nce the local executor reports success, the message is queued and any destination-side rejection results in trapped assets at the destination with no automatic recovery path": [3](#0-2) 

This is the direct structural analog of the ZetaChain issue: the ZRC20/Zeta burn on the source chain happened unconditionally, and the compensating mint on revert could fail (max-supply exceeded, receiver contract revert), after which the CCTX was marked `Aborted` and the burned value became permanently unrecoverable. Here, the pallet burns/withdraws value locally, considers the transfer `Ok`/settled based only on local queuing success, and if the destination's `DepositAsset` fails (e.g. the `StagingLocation`/beneficiary account is below existential deposit, gets filtered by the destination's barrier since this uses `UnpaidExecution`, or execution runs out of weight since `Weight::MAX` is passed and the call site's weight budget assumption is violated), the teleported-in assets land in the destination's `AssetTrap` under an aliased origin (`AliasOrigin(source_location)` from `preserve_origin: true`), which is not the same as the beneficiary account and has no automated relayer/claimer wired into this pipeline.

### Impact Explanation
This is autonomous, unprivileged-triggered value loss: `on_idle` runs automatically every `TransferPeriod` on production system chains (Westend relay, collectives-westend, coretime-westend, people-westend, bridge-hub-westend all wire this pallet in, per repo-wide references), accumulating transaction fees, dust removal, and coretime revenue. No governance or admin action is needed to trigger the loss — it occurs whenever the routine periodic forward's remote leg fails for any transient or configuration reason (insufficient ED at destination, barrier changes, congestion causing insufficient remote weight). The result is a genuine, permanent burn of protocol/treasury-bound revenue with no way to reissue it, since only the `Forwarder`/XCM message construction is aware of what happened, and the pallet has already emitted `ForwardSucceeded` and discarded any record needed to retry or claim the trapped assets.

### Likelihood Explanation
Likelihood is non-trivial because the vulnerable path is exercised by ordinary operation, not by an attacker: every periodic forward from every configured chain runs this exact sequence. The window for destination-side failure is real and acknowledged in-repo (the doc comment describing "trapped assets ... with no automatic recovery path" was written by the pallet authors themselves, confirming the failure mode is known but not mitigated with a retry/claim mechanism). Any transient destination-side condition — insufficient existential deposit at the staging account, congestion causing the unpaid barrier/weight assumptions to be violated, or a destination-side runtime upgrade changing acceptance rules — is sufficient to trigger fund loss on the next `on_idle` cycle.

### Recommendation
1. Do not treat local XCM completion as final settlement. Track pending forwards (e.g. store the amount/nonce) until a destination-side confirmation (delivery/success report, or a `QueryResponse`/notification) is received, only then finalizing accounting.
2. Avoid `UnpaidExecution` + `Weight::MAX` for the remote leg; use `BuyExecution`/paid execution and a bounded weight limit so failures are deterministic and can be detected/retried rather than silently trapping.
3. Ensure the staging/beneficiary account on the destination is guaranteed to satisfy ED requirements (e.g. pre-fund, or use `DepositAsset` with `AllCounted` fallback plus a guaranteed-success path), and add an automated claim/retry mechanism (e.g. a permissionless `claim_trapped` call plumbed to this pallet) so trapped assets are recoverable without requiring a hand-crafted `ClaimAsset` XCM from the exact aliased origin.
4. At minimum, do not burn/finalize local funds until destination success is confirmed — keep the funds in a "pending" state that can be re-forwarded if the destination leg fails.

### Proof of Concept
1. Configure a system chain (e.g. coretime-westend) with `pallet_accumulate_and_forward` forwarding accumulated fees to the relay chain's staging account via `TeleportForwarderForAccountId32`.
2. Accumulate fees/dust past `MinTransferAmount` in the accumulation account.
3. On the destination chain, ensure the `StagingLocation` beneficiary account is below ED (e.g. freshly derived pallet-id-derived account never funded), or otherwise arrange for the remote `DepositAsset` to fail (barrier/weight condition).
4. Wait for `on_idle` to trigger at the next `TransferPeriod` boundary: `forward()` executes locally, `WithdrawAsset`+teleport burn succeeds, `Outcome::Complete` is returned, `Ok(())` propagates, and `ForwardSucceeded` is emitted — even though the remote `DepositAsset` subsequently fails and the minted assets are trapped on the destination chain.
5. Observe: local total issuance is permanently reduced by `available_funds`, but no equivalent balance exists anywhere claimable by the runtime; the value is lost, matching the "funds burned, not remotely credited due to destination failure" pattern from the external report. [4](#0-3) [5](#0-4)

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L150-201)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<SystemBlockNumberFor<T>> for Pallet<T> {
		fn on_idle(_block: SystemBlockNumberFor<T>, remaining_weight: Weight) -> Weight {
			// Only attempt forwarding on blocks that are exact multiples of `TransferPeriod`.
			let block = T::BlockNumberProvider::current_block_number();
			if (block % T::TransferPeriod::get()) != Zero::zero() {
				return Weight::zero();
			}

			let mut meter = WeightMeter::with_limit(remaining_weight);

			// Need one read for the balance check.
			if meter.try_consume(T::DbWeight::get().reads(1)).is_err() {
				return meter.consumed();
			}

			let accumulation_account = Self::accumulation_account();
			// We use `reducible_balance` with `Preservation::Preserve` to get the
			// usable balance (excluding the ED).
			let available_funds = T::Currency::reducible_balance(
				&accumulation_account,
				Preservation::Preserve,
				Fortitude::Polite,
			);

			if available_funds < T::MinTransferAmount::get() {
				return meter.consumed();
			}

			// Ensure there is enough weight budget for the full XCM send.
			if meter.try_consume(T::WeightInfo::send_native()).is_err() {
				return meter.consumed();
			}

			// Attempt to forward accumulated funds.
			match T::Forwarder::forward(accumulation_account, available_funds) {
				Ok(()) => {
					Self::deposit_event(Event::ForwardSucceeded { amount: available_funds });
				},
				Err(()) => {
					log::debug!(
						target: LOG_TARGET,
						"accumulate-forward transfer of {:?} failed at block {:?}",
						available_funds,
						block,
					);
					Self::deposit_event(Event::ForwardFailed { amount: available_funds });
				},
			}

			meter.consumed()
		}
```

**File:** polkadot/xcm/xcm-builder/src/forwarder.rs (L34-106)
```rust
/// XCM adapter that implements [`pallet_accumulate_and_forward::Forwarder`] for AccountId32-type
/// source accounts by teleporting native tokens to a target account on a destination chain.
/// Local-execution failures roll back all local state changes. Once the local executor reports
/// success, the message is queued and any destination-side rejection results in trapped assets
/// at the destination with no automatic recovery path.
///
/// NOTE: This adapter passes `Weight::MAX` to the XCM executor, relying on the call site to
/// enforce a weight budget before invoking it. It is designed to be called only from rate-limited
/// internal hooks such as `on_idle` and should never be wired to user-callable extrinsics.
pub struct TeleportForwarderForAccountId32<XcmConfig, Dest, NativeAsset, StagingLocation>(
	PhantomData<(XcmConfig, Dest, NativeAsset, StagingLocation)>,
);

impl<XcmConfig, Dest, NativeAsset, StagingLocation, AccountId, Balance>
	pallet_accumulate_and_forward::Forwarder<AccountId, Balance>
	for TeleportForwarderForAccountId32<XcmConfig, Dest, NativeAsset, StagingLocation>
where
	XcmConfig: xcm_executor::Config,
	Dest: Get<Location>,
	NativeAsset: Get<Location>,
	StagingLocation: Get<InteriorLocation>,
	AccountId: Into<[u8; 32]> + Clone,
	Balance: Into<u128> + Copy,
{
	fn forward(source: AccountId, amount: Balance) -> Result<(), ()> {
		let dest = Dest::get();
		let asset = Asset { id: AssetId(NativeAsset::get()), fun: Fungible(amount.into()) };
		let beneficiary: Location = StagingLocation::get().into_location();

		let remote_xcm = Xcm(vec![DepositAsset { assets: Wild(AllCounted(1)), beneficiary }]);

		// The XCM flow: `ReceiveTeleportedAsset → AliasOrigin(source) → UnpaidExecution →
		// DepositAsset`. `preserve_origin: true` causes `InitiateTransfer` to prepend
		// `AliasOrigin(source_location)` to the remote XCM.
		let xcm: Xcm<XcmConfig::RuntimeCall> = Xcm(vec![
			UnpaidExecution { weight_limit: WeightLimit::Unlimited, check_origin: None },
			DescendOrigin(Junction::AccountId32 { network: None, id: source.into() }.into()),
			WithdrawAsset(asset.into()),
			InitiateTransfer {
				destination: dest,
				remote_fees: None,
				preserve_origin: true,
				assets: BoundedVec::truncate_from(alloc::vec![AssetTransferFilter::Teleport(
					Wild(AllCounted(1))
				),]),
				remote_xcm,
			},
		]);

		with_transaction(|| -> TransactionOutcome<Result<(), DispatchError>> {
			let outcome = XcmExecutor::<XcmConfig>::prepare_and_execute(
				Location::here(),
				xcm,
				&mut [0u8; 32],
				Weight::MAX,
				Weight::MAX,
			);

			match outcome {
				Outcome::Complete { .. } => TransactionOutcome::Commit(Ok(())),
				exec_error => {
					tracing::debug!(
						target: LOG_TARGET,
						?exec_error,
						"accumulate-forward: XCM execution failed"
					);

					TransactionOutcome::Rollback(Err(DispatchError::Other("XCM execution failed")))
				},
			}
		})
		.map_err(|_| ())
	}
```

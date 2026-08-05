### Title
`ProxyType::NonTransfer` in Asset Hub runtimes does not block `PolkadotXcm` calls, letting a "non-transfer" proxy move the delegator's funds - ([File: cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs])

### Summary
The Olympus report's core defect is that a hand-maintained "is this action high-risk" classification (a match/allow-list keyed on action/target type) omits certain state-changing operations (`MigrateKernel`, changes to the risk mapping itself) that are just as dangerous as the ones explicitly covered, letting an attacker perform a high-impact action while it is treated as low-risk. The same *broken-invariant class* — a manually enumerated call filter used to gate "high risk" capabilities (here: asset transfer) that misses a call path with equivalent effect — exists in the `ProxyType::NonTransfer` `InstanceFilter` implementation in the Asset Hub runtimes.

### Finding Description
`ProxyType::NonTransfer` is implemented as a **blocklist**: it returns `true` (call is allowed for a "non-transfer" proxy) for every `RuntimeCall` variant that is **not** explicitly matched: [1](#0-0) 

```
ProxyType::NonTransfer => !matches!(
    c,
    RuntimeCall::Balances { .. } |
        RuntimeCall::Assets { .. } |
        RuntimeCall::NftFractionalization { .. } |
        RuntimeCall::Nfts { .. } |
        RuntimeCall::Uniques { .. } |
        RuntimeCall::Scheduler(..) |
        RuntimeCall::Treasury(..) |
        RuntimeCall::Vesting(pallet_vesting::Call::vested_transfer { .. }) |
        RuntimeCall::ConvictionVoting(..) |
        RuntimeCall::Referenda(..) |
        RuntimeCall::Whitelist(..)
),
```

This is the same pattern used across the sibling runtimes (`asset-hub-rococo`, `staking-async/runtimes/parachain`), all sharing this same blocklist without a `RuntimeCall::PolkadotXcm(..)` entry: [2](#0-1) 

`RuntimeCall::PolkadotXcm` is never included in this exclusion list, even though the `pallet-xcm` dispatchables (`transfer_assets`, `limited_teleport_assets`, `limited_reserve_transfer_assets`, `teleport_assets`, `reserve_transfer_assets`, `execute`) move the caller's local balance of `Balances`/`Assets`/NFTs off-chain or to arbitrary destinations — exactly the class of action `NonTransfer` is meant to forbid. By contrast, the relay-chain runtimes (`polkadot/runtime/rococo`, `polkadot/runtime/westend`) implement `NonTransfer` as an **allowlist** and explicitly comment "Specifically omitting the entire XCM Pallet" to keep XCM out of scope: [3](#0-2) 

This shows the intended design invariant ("NonTransfer proxies must not be able to move funds via XCM either") but the Asset Hub blocklist-style filter, which must enumerate every dangerous call individually, fails to enforce it, exactly like the Olympus classifier failing to add `MigrateKernel`/risk-mapping changes to its high-risk set.

### Impact Explanation
`pallet-proxy`'s `proxy`/`proxy_announced` dispatch is a public entry point available to any account that has been granted a `NonTransfer` proxy — a delegation explicitly intended to be safe from asset movement (used, e.g., for governance/voting-only delegates). Because `PolkadotXcm` calls are not filtered out, the delegate (who never received transfer rights) can invoke `PolkadotXcm::transfer_assets`, `limited_teleport_assets`, or `limited_reserve_transfer_assets` through the proxy to send the delegator's `Balances`/`Assets`/NFT holdings to any destination account/chain, or use `PolkadotXcm::execute` to run an arbitrary XCM program with `WithdrawAsset`/`DepositAsset` instructions from the proxied origin. This is unauthorized execution / theft of proxied funds through a public wrapper that was supposed to widen only non-transfer capabilities, matching the required "theft or unbacked mint or unlock" / "unauthorized execution or origin escalation" impact classes.

### Likelihood Explanation
No privileged actor is required: the attack works with any address that has been added as a `NonTransfer` proxy by a legitimate account owner (a routine, expected delegation for e.g. governance-only use), and the caller of `Proxy::proxy` is the delegate itself, not the delegator. The `PolkadotXcm` pallet dispatchables are always present and callable in Asset Hub runtimes, so exploitation requires no additional preconditions beyond an existing `NonTransfer` proxy relationship, which is a normal/expected configuration in these runtimes.

### Recommendation
Add `RuntimeCall::PolkadotXcm(..)` (and any other pallet capable of moving the caller's assets, e.g. `AssetConversion` swaps that could be chained with XCM) to the `NonTransfer` exclusion list in all Asset Hub / parachain runtimes that use the blocklist style filter, or switch these runtimes to the relay-chain's allowlist style filter (`matches!` on an explicit safe set) so that newly added pallets/calls are excluded by default rather than included by default.

### Proof of Concept
1. Account `A` grants account `B` a `pallet_proxy::Call::add_proxy` delegation with `ProxyType::NonTransfer` on Asset Hub (Westend/Rococo).
2. `B` (holding no direct rights over `A`'s balance) submits:
   `Proxy::proxy(RuntimeOrigin::signed(B), A, Some(ProxyType::NonTransfer), Box::new(RuntimeCall::PolkadotXcm(pallet_xcm::Call::limited_teleport_assets { dest: ..., beneficiary: B_or_attacker_controlled, assets: A's_full_balance, fee_asset_item: 0, weight_limit: Unlimited })))`.
3. `ProxyType::NonTransfer::filter` evaluates `!matches!(c, Balances{..} | Assets{..} | ... )` — since the call is `RuntimeCall::PolkadotXcm(..)`, none of the listed patterns match, so the filter returns `true` and the call is dispatched with `A`'s origin.
4. `A`'s tokens are teleported/reserve-transferred out under `B`'s control, despite `B` only ever being authorized for "non-transfer" actions — proving the `NonTransfer` invariant is broken via an unlisted high-risk call path.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L834-849)
```rust
			ProxyType::NonTransfer => !matches!(
				c,
				RuntimeCall::Balances { .. } |
					RuntimeCall::Assets { .. } |
					RuntimeCall::NftFractionalization { .. } |
					RuntimeCall::Nfts { .. } |
					RuntimeCall::Uniques { .. } |
					RuntimeCall::Scheduler(..) |
					RuntimeCall::Treasury(..) |
					// We allow calling `vest` and merging vesting schedules, but obviously not
					// vested transfers.
					RuntimeCall::Vesting(pallet_vesting::Call::vested_transfer { .. }) |
					RuntimeCall::ConvictionVoting(..) |
					RuntimeCall::Referenda(..) |
					RuntimeCall::Whitelist(..)
			),
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L612-623)
```rust
impl InstanceFilter<RuntimeCall> for ProxyType {
	fn filter(&self, c: &RuntimeCall) -> bool {
		match self {
			ProxyType::Any => true,
			ProxyType::NonTransfer => !matches!(
				c,
				RuntimeCall::Balances { .. } |
					RuntimeCall::Assets { .. } |
					RuntimeCall::NftFractionalization { .. } |
					RuntimeCall::Nfts { .. } |
					RuntimeCall::Uniques { .. }
			),
```

**File:** polkadot/runtime/rococo/src/lib.rs (L930-975)
```rust
			ProxyType::NonTransfer => matches!(
				c,
				RuntimeCall::System(..) |
				RuntimeCall::Babe(..) |
				RuntimeCall::Timestamp(..) |
				RuntimeCall::Indices(pallet_indices::Call::claim {..}) |
				RuntimeCall::Indices(pallet_indices::Call::free {..}) |
				RuntimeCall::Indices(pallet_indices::Call::freeze {..}) |
				// Specifically omitting Indices `transfer`, `force_transfer`
				// Specifically omitting the entire Balances pallet
				RuntimeCall::Session(..) |
				RuntimeCall::Grandpa(..) |
				RuntimeCall::Treasury(..) |
				RuntimeCall::Bounties(..) |
				RuntimeCall::ChildBounties(..) |
				RuntimeCall::ConvictionVoting(..) |
				RuntimeCall::Referenda(..) |
				RuntimeCall::FellowshipCollective(..) |
				RuntimeCall::FellowshipReferenda(..) |
				RuntimeCall::Whitelist(..) |
				RuntimeCall::Claims(..) |
				RuntimeCall::Utility(..) |
				RuntimeCall::Identity(..) |
				RuntimeCall::Society(..) |
				RuntimeCall::Recovery(pallet_recovery::Call::set_friend_groups {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::initiate_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::approve_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::finish_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::cancel_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::slash_attempt {..}) |
				// Specifically omitting Recovery `control_inherited_account`
				RuntimeCall::Vesting(pallet_vesting::Call::vest {..}) |
				RuntimeCall::Vesting(pallet_vesting::Call::vest_other {..}) |
				// Specifically omitting Vesting `vested_transfer`, and `force_vested_transfer`
				RuntimeCall::Scheduler(..) |
				RuntimeCall::Proxy(..) |
				RuntimeCall::Multisig(..) |
				RuntimeCall::Nis(..) |
				RuntimeCall::Registrar(paras_registrar::Call::register {..}) |
				RuntimeCall::Registrar(paras_registrar::Call::deregister {..}) |
				// Specifically omitting Registrar `swap`
				RuntimeCall::Registrar(paras_registrar::Call::reserve {..}) |
				RuntimeCall::Crowdloan(..) |
				RuntimeCall::Slots(..) |
				RuntimeCall::Auctions(..) // Specifically omitting the entire XCM Pallet
			),
```

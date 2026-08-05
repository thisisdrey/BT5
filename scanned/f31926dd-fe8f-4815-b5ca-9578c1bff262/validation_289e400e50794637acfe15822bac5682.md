### Title
Silent loss of excess ERC20 assets in `ERC20Transactor::deposit_asset_with_surplus` - (File: `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
`ERC20Transactor::deposit_asset_with_surplus` accepts a full `AssetsInHolding` collection but only ever processes the *first* fungible asset it finds via `.fungible_assets_iter().next()`. Any additional fungible assets contained in the same `what` value are neither deposited to the beneficiary nor returned to the caller — they are simply dropped when the function returns `Ok(surplus)`. This mirrors the core defect in the referenced Line-of-Credit report: value attached to a call (`msg.value` there, the extra assets in the holding register here) that exceeds what the internal accounting path consumes is never refunded and becomes permanently unrecoverable.

### Finding Description
The trait method signature takes the whole `AssetsInHolding` (not a single `Asset`), which is the batch of assets the XCM executor has matched to this asset transactor for a `DepositAsset` instruction: [1](#0-0) 

The comment on the function itself admits the limitation: "this implementation only handles a single fungible asset at a time... If multiple assets are present, only the first fungible asset will be deposited and the rest will be silently ignored." The only safeguard is a `defensive_assert!(what.len() == 1, ...)`, which is a no-op in production/release builds — it does not abort execution or return an error, it only logs in debug builds: [2](#0-1) 

Once the single matched `(asset_contract_id, amount)` is extracted and the ERC20 transfer succeeds, the function returns `Ok(surplus)`: [3](#0-2) 

`what` (the whole `AssetsInHolding`, owned by value) is dropped at that point. Unlike the failure paths, which return `Err((what, ...))` so the caller can trap the assets via `AssetsTrapped` for later reclaim, the success path never returns `what`, so any leftover assets it still contained are unconditionally destroyed with no accounting record and no way for the XCM originator/beneficiary to reclaim them.

This is directly analogous to the reported bug class: an entry point receives more value than it consumes internally (`msg.value > amount` there; extra fungible assets in `what` here), and the excess is neither refunded nor tracked, resulting in permanent loss.

### Impact Explanation
If the XCM executor ever invokes this method with an `AssetsInHolding` containing more than one fungible asset routed through the ERC20 asset-transactor slot (e.g. a `DepositAsset { assets: Wild(All) or Definite([...]) , beneficiary }` instruction whose filter matches multiple ERC20 tokens, or an ERC20 token plus another fungible asset also handled by this transactor instance), all assets beyond the first are permanently and silently burned — not credited to the beneficiary, not trapped for later `claim_assets`, and not reflected in any error or event. This is unbacked loss of user funds with no privileged actor or malicious peer required; a normal XCM program constructed by an ordinary user (or a cross-chain message, including from Snowbridge inbound flows that route ERC20 assets through `pallet_revive`) can trigger it.

### Likelihood Explanation
Reachability depends on whether the XCM executor's asset-transactor dispatch for `DepositAsset` can hand this transactor an `AssetsInHolding` batch with more than one qualifying fungible asset in a single call (rather than being pre-split into one-asset calls upstream). The function's own doc comment and the `defensive_assert!` indicate the authors anticipated — and considered possible — exactly this multi-asset misuse case, which is itself evidence that the invariant "always exactly one asset" is not structurally guaranteed by the trait/caller contract, only assumed. Given `defensive_assert!` compiles to a no-op outside debug builds, production runtimes have no functional guard at all.

### Recommendation
- Have `deposit_asset_with_surplus` iterate over *all* fungible assets in `what`, attempting delivery for each, or explicitly reject (return `Err((what, XcmError::AssetNotHandled))`) whenever `what.len() != 1` so the full, untouched holding is returned to the caller and can be trapped/refunded instead of silently destroyed.
- Do not rely on `defensive_assert!` as a security boundary; replace it with a genuine runtime check that fails safely (returning ownership of `what`) rather than merely logging in debug builds.
- Add a regression test that sends a `DepositAsset` XCM instruction whose asset filter matches two or more assets handled by `ERC20Transactor`, and assert that either all assets are delivered or all are returned/trapped — never partially destroyed.

### Proof of Concept
1. Configure a runtime where `ERC20Transactor` is included as (one of) the `AssetTransactor`s and is reachable for at least two distinct ERC20 tokens (or one ERC20 token plus another asset class matched by the same `Matcher`).
2. Craft an XCM program that ends in `DepositAsset { assets: Wild(AllCounted(2)) /* or Definite([asset_a, asset_b]) */, beneficiary }` such that both `asset_a` and `asset_b` are routed to this `ERC20Transactor` instance in a single `deposit_asset_with_surplus` call with a holding register containing both.
3. Execute the XCM program via `pallet_xcm` / `polkadotXcm::execute` (or an inbound Snowbridge message using the ERC20 asset transactor) with a holding register funded with both assets.
4. Observe: the beneficiary's ERC20 balance increases only by `asset_a`'s amount; `asset_b`'s amount is neither credited to the beneficiary nor emitted as `AssetsTrapped`; it disappears from all runtime state with `Ok(surplus)` returned as if the whole deposit succeeded.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-243)
```rust
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		tracing::trace!(
			target: "xcm::transactor::erc20::deposit",
			?what, ?who,
		);
		defensive_assert!(what.len() == 1, "Trying to deposit more than one asset!");
		// Check we handle this asset.
		let maybe = what
			.fungible_assets_iter()
			.next()
			.and_then(|asset| Matcher::matches_fungibles(&asset).ok());
		let (asset_contract_id, amount) = match maybe {
			Some(inner) => inner,
			None => return Err((what, MatchError::AssetNotHandled.into())),
		};
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L276-280)
```rust
				match IERC20::transferCall::abi_decode_returns_validate(&return_value.data) {
					Ok(true) => {
						tracing::trace!(target: "xcm::transactor::erc20::deposit", "ERC20 contract was successful");
						Ok(surplus)
					},
```

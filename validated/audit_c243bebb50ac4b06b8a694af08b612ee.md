Audit Report

## Title
Missing slippage protection in Snowbridge `system-frontend` tip/fee swap enables sandwich attacks that under-fund relayer rewards - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`swap_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` invokes `T::Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, disabling the slippage-protection check (`Error::<T>::ProvidedMinimumNotSufficientForSwap`) that `pallet_asset_conversion` otherwise provides. This code path is reachable from two unprivileged, public extrinsics — `add_tip` and `register_token` (non-root origin) — both of which convert a user-supplied fee/tip asset into ether whose resulting amount is burned and forwarded as the relayer reward `amount` in an `EthereumSystemCall::AddTip`/`RegisterToken` XCM `Transact` to BridgeHub.

## Finding Description
`swap_and_burn` calls `T::Swap::swap_exact_tokens_for_tokens(who, swap_path, tip_amount, None, who, true)` with the minimum-output parameter explicitly set to `None` and a comment "No minimum amount required" (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:301-308`). In `pallet_asset_conversion`'s `do_swap_exact_tokens_for_tokens`, this check is only applied `if let Some(amount_out_min) = amount_out_min` (`substrate/frame/asset-conversion/src/lib.rs:989-1002`) — since the caller never supplies `Some`, the guard is unconditionally skipped and the swap accepts any non-zero output.

This function is reached via `swap_fee_asset_and_burn`, called by:
- `add_tip(origin, message_id, asset)` for any signed account (`lib.rs:261-273`).
- `register_token(origin, asset_id, metadata, fee_asset)` for any non-root/non-`Here` origin (`lib.rs:225-252`).

The resulting `ether_gained` — whatever the AMM pool happens to return, however degraded by price impact — is burned via `burn_for_teleport` and then forwarded verbatim as the `amount` field of the `AddTip`/`RegisterToken` XCM `Transact` call sent to BridgeHub (`lib.rs:340-351`), where BridgeHub's `EthereumSystem` pallet credits it as the relayer reward for `message_id`. No re-validation or minimum-bound re-check occurs on the BridgeHub side against an expected/fair value.

## Impact Explanation
Because there is no floor on the swap output, an actor able to influence the AMM pool's price at the time this extrinsic executes (e.g., by submitting a large swap in the same pool ordered adjacent to the victim's `add_tip`/`register_token` call) can cause the pallet to accept an arbitrarily degraded exchange rate. The tip payer's `fee_asset`/tip asset is fully consumed while the ether amount credited as the relayer reward on BridgeHub is reduced — this is value loss to the caller and results in an under-funded reward being recorded for the corresponding message, which is the exact "public underpriced work" pattern (degraded, attacker-influenced payout amount forwarded into bridge settlement) called out in the impact gate.

## Likelihood Explanation
Both `add_tip` and `register_token` are unprivileged, publicly callable extrinsics; the swap path, pool, and amount are all attacker-visible before execution, and `pallet_asset_conversion` pools used for tip assets are plausibly shallow relative to the tip amounts. No compromised relayer, validator, collator, or governance role is required — this is exploitable purely through ordinary extrinsic submission/ordering, differing from the disallowed "front-run-only" category because the attacker's back-run to recapture the price impact (the second leg of a sandwich) is also entirely achievable via ordinary public transaction submission.

## Recommendation
Compute and pass a non-`None` `amount_out_min` in `swap_and_burn`, derived either from a fair-price quote (e.g., `T::Swap`/`QuotePrice::quote_price_exact_tokens_for_tokens`) with a bounded slippage tolerance, or from a caller-supplied minimum threaded through `add_tip`/`register_token`, so that a manipulated pool price causes the swap to fail with `ProvidedMinimumNotSufficientForSwap` rather than silently returning a degraded amount that is burned and forwarded to BridgeHub.

## Proof of Concept
1. Attacker identifies a pending `add_tip` (or `register_token`) call against a thinly-liquid `tip_asset → ether` pool in `pallet_asset_conversion`.
2. Attacker submits a swap ordered immediately before the victim's call to move the pool price against the tip asset.
3. Victim's call executes `swap_and_burn` with `amount_out_min = None` (`lib.rs:305`), so it succeeds despite yielding far less ether than a fair-price quote.
4. Attacker reverses their initial swap to recapture the price-impact profit.
5. The under-valued `ether_gained` is burned and transmitted as the `amount` in `EthereumSystemCall::AddTip`/`RegisterToken` to BridgeHub (`lib.rs:340-351`), permanently under-funding the relayer reward for that message while the victim's tip asset is fully consumed. This can be reproduced as a unit test in the `system-frontend` pallet's mock runtime by configuring a pool with intentionally skewed reserves before invoking `add_tip` and asserting the reduced `ether_gained` versus a fair-price expectation.
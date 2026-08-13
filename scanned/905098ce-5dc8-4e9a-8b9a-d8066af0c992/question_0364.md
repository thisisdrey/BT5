# Q364: change_asset_shares: rounding boundary creates extractable dust [a-withdraw-that-targets-an] [cycle]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a withdraw that targets an account near initial-health failure to push `change_asset_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and eventually causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw that targets an account near initial-health failure
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.

# Q3234: lending_account_repay: rounding boundary creates extractable dust [a-repay-amount-at-the] [cycle]

## Question
Can an unprivileged attacker use `lending_account_repay` with a repay amount at the last-liability-share boundary to push `lending_account_repay` across a rounding edge where protocol totals and user shares no longer match, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and eventually causing `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount at the last-liability-share boundary
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.

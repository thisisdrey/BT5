# Q3977: lending_account_liquidate: liquidation phase ordering bypasses instruction exclusivity [tiny-residual-assets-liabilities-around] [phase-replay]

## Question
Can an unprivileged attacker combine `lending_account_liquidate` with tiny residual assets/liabilities around zero thresholds so `lending_account_liquidate` bypasses intended instruction ordering or exclusivity, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: tiny residual assets/liabilities around zero thresholds
- Exploit idea: Attack any assumption that liquidation start/end must be first/last/exclusive in the instruction list to ensure no hidden side effects happen mid-session. Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Assemble adversarial transactions around the phase boundary and assert exclusivity checks reject every mixed ordering that would change economics. Execute start/end phases with replay and reorder attempts and assert no extra seize, skipped repay, or stuck flag survives.

# Q3976: lending_account_liquidate: liquidation phase ordering bypasses instruction exclusivity [a-same-slot-price-cache] [threshold]

## Question
Can an unprivileged attacker combine `lending_account_liquidate` with a same-slot price-cache or health-cache change before liquidation so `lending_account_liquidate` bypasses intended instruction ordering or exclusivity, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a same-slot price-cache or health-cache change before liquidation
- Exploit idea: Attack any assumption that liquidation start/end must be first/last/exclusive in the instruction list to ensure no hidden side effects happen mid-session. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Assemble adversarial transactions around the phase boundary and assert exclusivity checks reject every mixed ordering that would change economics. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.

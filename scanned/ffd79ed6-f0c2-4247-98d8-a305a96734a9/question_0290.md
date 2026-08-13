# Q290: get_liability_shares: cache refresh ordering permits stale acceptance [a-borrow-amount-at-the] [cycle]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow amount at the smallest non-zero liability-share boundary so `get_liability_shares` accepts a state transition using stale cache values before refresh or recomputation, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow amount at the smallest non-zero liability-share boundary
- Exploit idea: Audit whether the instruction depends on cached bank/account state that can lag behind the exact balances or prices settled later in the same path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Prepare mismatched cache and live state, invoke the instruction, and assert acceptance never occurs unless recomputed state would still satisfy the rules. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.

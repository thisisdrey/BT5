### Title
`system-repay` divides by `total-debt` without a zero-guard, reverting (and freezing the repay path) once the last borrower's scaled principal reaches zero - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
The Alchemix `[M-16]` finding is about a state where `totalShares`/credit-tracking denominators can hit zero while there is still outstanding value to account for, causing every downstream function that divides by that denominator to revert and freezing the contract. The Zest vault contracts have a structurally identical pattern in `system-repay`: it divides by `debt` (derived from `principal-scaled`) without checking that `debt` is non-zero, unlike the vault's own share-conversion helpers (`convert-to-shares-preview`/`convert-to-assets-preview`) which explicitly guard the zero-denominator case.

### Finding Description
Each vault (`v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) tracks aggregate borrower debt via `principal-scaled` and an interest `index`: [1](#0-0) 

`system-repay` is called by the market contract (via `check-caller-auth`) whenever a borrower repays debt on this vault: [2](#0-1) 

Here `debt` is `(total-debt)`, i.e. `calc-cumulative-debt(principal-scaled, index)`. When `principal-scaled` is `0` (the natural end state after the last active borrower on this vault fully repays), `debt` becomes `0`. `capped-amount` is then correctly clamped to `0` (`if (> amount debt) debt amount`), but the very next binding,

```
(principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
```

divides by `debt`, which is `0`. Unlike `convert-to-shares-preview` / `convert-to-assets-preview`, which explicitly special-case a zero `total-supply`/`total-assets` denominator before dividing: [3](#0-2) 

`system-repay`'s `principal-repaid` calculation has no such guard, so a division-by-zero occurs. This mirrors the exact Alchemix root cause: a per-token/per-vault aggregate denominator (there `totalShares`, here `principal-scaled`/`total-debt`) that legitimately reaches zero once the last position is closed, while a later function in the same accounting family still unconditionally divides by it.

The reachability path from a normal principal:
1. A borrower opens debt on a given vault (e.g. `vault-stx`) via the market, driving `principal-scaled` > 0.
2. The borrower (or the last remaining borrower on that vault) fully repays, driving `principal-scaled` to exactly `0` and `total-borrowed` to `0` (there is no dust/floor enforced, since `updated-scaled-principal` is a straightforward subtraction, and `capped-reduction` can equal `scaled-principal`).
3. Any subsequent call into `system-repay` for that vault (even with `amount > 0` supplied by a well-meaning caller, e.g. paying back accrued but already-zeroed dust, or via market retry logic) with `debt == 0` triggers `mul-div-down capped-amount total-borrowed-amount debt` with `debt = 0`, causing the transaction to abort with a division-by-zero runtime error.

### Impact Explanation
This is a temporary freezing-of-funds condition scoped to the affected vault's repay entry point: once `principal-scaled`/`total-debt` for a vault legitimately reach zero, any market-routed `system-repay` call against that vault reverts instead of completing, rather than failing gracefully or completing a no-op repayment. Depending on how the market's repay flow composes calls (e.g., batched repay of multiple positions, or retried repay after full closure), this can block otherwise-valid user repay transactions and lock the associated flow until the vault re-accrues nonzero debt, which qualifies as a temporary freezing of funds under the impact rubric.

### Likelihood Explanation
The zero-debt state is a completely ordinary outcome of normal protocol usage (last borrower on a vault repaying in full) and requires no privileged action or edge-case market manipulation — it is the same "no distinct users left" scenario the original Alchemix report described. The triggering condition (calling `system-repay` again while `debt == 0`) depends on exact market-side call sequencing/guards, which were not fully verifiable within the indexed context (the `v0-4-market.clar` repay dispatch logic that decides whether/when to call `system-repay` on a fully-repaid vault was not retrievable in this session), so likelihood is assessed as plausible but not confirmed end-to-end.

### Recommendation
Add an explicit zero-denominator guard in `system-repay` mirroring the pattern already used in `convert-to-shares-preview`/`convert-to-assets-preview`: short-circuit `principal-repaid` (and the whole repay logic) to `0`/no-op when `debt` is `0`, instead of unconditionally calling `mul-div-down capped-amount total-borrowed-amount debt`.

### Proof of Concept
Conceptual sequence, applicable to any of the six vaults (`v0-vault-stx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`):
1. Market calls `system-borrow` on the vault, setting `principal-scaled > 0`.
2. Borrower fully repays via `system-repay`, driving `principal-scaled` and `total-borrowed` to `0` (`debt` becomes `0` on next accrual).
3. Market (or a retried/duplicate repay call) invokes `system-repay` again with `amount > 0` while `debt == 0`.
4. `capped-amount` correctly resolves to `0`, but `(mul-div-down capped-amount total-borrowed-amount debt)` divides by `debt = 0`, aborting the transaction — because, unlike the vault's own conversion helpers, this line has no zero-denominator branch.

Full confirmation of exploitability requires tracing the market contract's exact repay dispatch conditions, which could not be completely verified with the available index; a Devin session with full repo access could confirm whether the market can actually re-invoke `system-repay` on a zero-debt vault in practice.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-324)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))

(define-private (convert-to-assets-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ta u0)
        u0
        (if (is-eq ts u0)
            u0
            (mul-div-down amount ta ts)))))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L328-332)
```text
(define-private (total-debt)
  (calc-cumulative-debt (var-get principal-scaled) (var-get index)))

(define-private (debt-preview)
  (calc-cumulative-debt (var-get principal-scaled) (next-index)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-920)
```text
(define-public (system-repay (amount uint))
  (let (
        (states (var-get pause-states))
        (u (try! (accrue)))
        (scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```

### Title
`vault-*.clar` `system-repay` silently truncates the accepted repayment to its own aggregate debt and discards the actual accepted amount, while `market.clar` still removes the full scaled debt from the borrower - (File: `mainnet/contracts/vault/v0-vault-stx.clar`, `mainnet/contracts/market/v0-4-market.clar`)

### Summary
This mirrors the Lido `LidoExecutionLayerRewardsVault.withdrawRewards` pattern: an internal function computes a saturated/capped "actual" amount but returns only a boolean, and the caller uses its own requested/expected amount for accounting instead of the value the callee actually accepted/processed.

### Finding Description
Every vault's `system-repay` computes `capped-amount` as `min(amount, debt)`, where `debt` is the vault's own aggregate `total-debt` (across all borrowers of that asset), and only pulls in `capped-amount` of underlying via `receive-underlying`: [1](#0-0) 

Crucially, `system-repay` returns only `(ok true)` — the actual `capped-amount` that was pulled in and used to update `principal-scaled`/`total-borrowed`/`assets` is never surfaced to the caller: [2](#0-1) 

`market.clar`'s routing wrapper `vault-system-repay` is typed to return only `(response bool uint)`, confirming the actual repaid amount is discarded at the interface boundary: [3](#0-2) 

In `repay`, the market computes `amount-to-repay` from the borrower's own scaled debt and the shared borrow index, calls `vault-system-repay` with that amount, and — regardless of what the vault actually accepted — unconditionally removes `repaid-scaled-debt` from the borrower's position in `market-vault`: [4](#0-3) 

The same pattern is used in `liquidate`, where `debt-to-repay` is passed to `vault-system-repay` and then `scaled-to-remove` is unconditionally cleared from the borrower via `market-vault` `debt-remove-scaled`, irrespective of the vault's own capped amount: [5](#0-4) 

The vault's `capped-amount` is bounded by the vault's *aggregate* `total-debt` across all borrowers of that asset, not by the individual borrower's debt. Under normal single-borrower operation `amount-to-repay ≤ debt` always holds (the aggregate is at least as large as any individual balance), so the cap is invisible. But `total-borrowed`/`principal-scaled` are also mutated independently by `socialize-debt` (bad-debt write-down during liquidation) and by rounding in `mul-div-down`/`mul-div-up` operations across `system-borrow`, `system-repay`, and `socialize-debt`. Since the vault's aggregate debt tracking and the market's per-borrower scaled-debt bookkeeping (`market-vault`) are two independently-rounded ledgers that are never reconciled through the `system-repay` return value, any accumulated drift where the vault's aggregate `debt` becomes smaller than what a specific borrower's own computed `amount-to-repay` implies will cause the vault to silently accept less underlying than the market believes was repaid — while the market still deletes the full scaled debt from that borrower's position.

### Impact Explanation
If the vault accepts less underlying (`capped-amount < amount-to-repay`) than what the market removes from the borrower's on-chain debt ledger, the vault's `assets`/`total-borrowed` state under-collects real value while the corresponding debt obligation is erased system-wide. This is the "buffered ETH counter goes out-of-sync and ETH gets lost" analog: value that should back outstanding zToken supply / vault assets disappears from the vault's books, degrading the vault's solvency (its `assets` will not match what depositors are owed), which falls under protocol insolvency / permanent freezing/loss of funds for other users of the vault.

### Likelihood Explanation
Under isolated single-repay conditions this is not directly triggerable, since `capped-amount` normally equals the requested amount (aggregate debt ≥ individual debt). The drift requires a divergence between the vault's own rounding-affected aggregate debt tracking and the market's independent per-borrower scaled-debt tracking (introduced by `socialize-debt` write-downs and repeated `mul-div-down`/`mul-div-up` rounding across many operations), which is a multi-step, compounding condition rather than a single-transaction trivial exploit, so likelihood is Low/Medium rather than High.

### Recommendation
Have `system-repay` (and equivalently `system-borrow`) return the actual `capped-amount` it processed, and update `repay`/`liquidate` in `market.clar` to use that returned value — instead of the caller's originally computed `amount-to-repay`/`debt-to-repay` — when calling `debt-remove-scaled` on `market-vault`. This mirrors the recommended Lido fix of using the vault's actual returned amount rather than trusting the requested maximum.

### Proof of Concept
1. Drive divergence between vault aggregate debt and market's per-borrower scaled debt through repeated `socialize-debt` calls during liquidations with no remaining collateral (each write-down independently rounds `principal-reduction` at the vault vs. `scaled-to-remove` at the market).
2. Over multiple such liquidation cycles for different borrowers on the same asset, accumulate rounding drift until the vault's `debt` (aggregate) computed via `total-debt` is smaller than a specific borrower's own `max-repay-tokens`.
3. That borrower calls `repay` with `amount-to-repay` slightly exceeding the vault's aggregate `debt`; `system-repay` silently caps at `capped-amount < amount-to-repay` and only pulls in the smaller amount, while `market.clar` still removes the borrower's full `repaid-scaled-debt`, permanently erasing debt without collecting the corresponding underlying.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-925)
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

    (try! (receive-underlying capped-amount tx-sender))
    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed total-borrowed-new)
    (var-set assets (+ (var-get assets) interest-paid))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L927-943)
```text
    (print {
      action: "system-repay",
      caller: contract-caller,
      data: {
        amount-requested: amount,
        amount-repaid: capped-amount,
        principal-repaid: principal-repaid,
        interest-paid: interest-paid,
        principal-scaled: updated-scaled-principal,
        total-borrowed: total-borrowed-new,
        assets: (var-get assets),
        index: idx
      }
    })

    (ok true)))

```

**File:** local-testing/tests/clarigen-types.ts (L1860-1860)
```typescript
    vaultSystemRepay: {"name":"vault-system-repay","access":"private","args":[{"name":"aid","type":"uint128"},{"name":"amount","type":"uint128"},{"name":"ft","type":"trait_reference"},{"name":"ft-address","type":"principal"}],"outputs":{"type":{"response":{"ok":"bool","error":"uint128"}}}} as TypedAbiFunction<[aid: TypedAbiArg<number | bigint, "aid">, amount: TypedAbiArg<number | bigint, "amount">, ft: TypedAbiArg<string, "ft">, ftAddress: TypedAbiArg<string, "ftAddress">], Response<boolean, bigint>>,
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1355-1361)
```text
    (try! (vault-system-repay asset-id amount-to-repay ft address))
    ;; update
    (try! (contract-call? .v0-market-vault
                            debt-remove-scaled
                            account
                            repaid-scaled-debt
                            asset-id))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1495-1503)
```text
    ;; execute liquidation
    (try! (vault-system-repay debt-aid debt-to-repay debt-ft debt-address))

    ;; update obligations and socialize bad debt
    (let ((debt-updated (try! (contract-call? .v0-market-vault
                              debt-remove-scaled
                              borrower
                              scaled-to-remove
                              debt-aid)))
```

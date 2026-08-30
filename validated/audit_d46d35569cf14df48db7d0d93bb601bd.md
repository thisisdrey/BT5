### Title
Fee-skimming/short-transfer underlying token breaks `assets`/share accounting invariant in vault `deposit`/`redeem` - ([File: mainnet/contracts/vault/v0-vault-usdc.clar], and equivalent v0-vault-sbtc.clar, v0-vault-ststx.clar, v0-vault-ststxbtc.clar, v0-vault-stx.clar, v0-vault-usdh.clar)

### Summary
The vault contracts credit deposits and debit redemptions based on the nominal `amount` parameter rather than the actual token amount the vault contract received/sent, mirroring the reported `AdvancedOrderEngine.fillOrders()` fee-on-transfer issue: if the underlying SIP-010 token contract ever skims a fee or otherwise delivers less than the requested `amount` on `transfer`, the vault's internal `assets` accounting and share math become permanently decoupled from the vault's real token balance.

### Finding Description
`deposit` computes shares from the nominal `amount`, calls `receive-underlying` (a plain `contract-call? ... transfer amount account current-contract none` to the external, DAO-whitelisted underlying token), and then unconditionally increments the internal `assets` variable by the full nominal `amount` regardless of what was actually received: [1](#0-0) 

`receive-underlying`/`send-underlying` never compare balances before/after the transfer; they just perform the call-out and return `ok true`: [2](#0-1) 

`convert-to-shares-preview`/`convert-to-assets-preview` (used for both `deposit` and `redeem` share math) derive their ratio purely from `total-assets-preview`/`total-supply-preview`, i.e. from the internal `assets` var and share supply, not from a live on-chain balance check of the underlying token contract: [3](#0-2) 

`redeem` similarly burns shares for `amount`, computes `inkind` from the (potentially now-inflated) `assets` bookkeeping variable, and calls `send-underlying` to push out `inkind` tokens, again without verifying the recipient actually received `inkind` (a fee-skimming underlying could deliver less than `inkind` to the recipient while the vault still debits its books as if the full `inkind` left): [4](#0-3) 

This is the same root-cause class as the reported issue: any calculation (share minting on deposit, `assets` bookkeeping, share redemption ratio) is done in terms of "amount requested to transfer" instead of "amount actually moved," so a discrepancy between requested and actual transferred amount (fee, faulty/blacklist-partial transfer, or any deviation from 1:1 semantics in the underlying token) is silently absorbed into the vault's solvency invariant rather than rejected or reconciled.

### Impact Explanation
If the whitelisted underlying token (e.g. the wrapped `usdcx`/`sbtc`/`ststx`/`stx`/`usdh` tokens referenced by `UNDERLYING`) is ever upgraded/migrated or replaced by a token whose SIP-010 `transfer` implementation deducts a fee — or transiently misbehaves and short-transfers — the vault mints shares/credits `assets` for the full nominal amount while holding strictly less real value. Because share price (`convert-to-assets-preview`) is derived from this inflated `assets` bookkeeping rather than the vault's true balance, later redeemers can withdraw more real underlying than the vault actually received in aggregate, draining honest depositors' funds — this is a protocol insolvency / permanent freezing-of-funds condition on the vault (the last redeemers cannot get funds), matching the report's stated invariant break (`balance[before] == balance[after]` for the vault). This lands in the in-scope "vault share math and interest accrual" category.

### Likelihood Explanation
Likelihood is Low-to-Medium under current conditions, matching the original report's rated severity of Medium: the client's own defense states tokens are DAO-whitelisted and that USDT-style fee tokens are the only known concern, with a contract-redeploy fallback plan. The underlying tokens currently wired (`usdcx`, wrapped `sbtc`, `ststx`, `stx`, `usdh`) are not currently fee-charging. However, since the vault is generic tokenized-vault logic reused across six deployed vault instances and the DAO can register new underlying assets/migrate wrapped tokens over time, the exposure recurs for every future asset onboarded through this exact pattern, and the code path performing no actual-received/actual-sent verification is squarely reachable by an ordinary depositor/redeemer once such a token is in use.

### Recommendation
In `receive-underlying` and `send-underlying` (and the corresponding functions in each vault: `v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdh.clar`), measure the underlying contract's balance-of-`current-contract` immediately before and after the `transfer` call and use the observed delta (not the nominal `amount`) for `ft-mint?`/`ft-burn?` share math and for updating the `assets` data-var, or explicitly assert that the delta equals `amount` and abort the deposit/redeem otherwise.

### Proof of Concept
1. DAO onboards or migrates the vault's `UNDERLYING` constant to a SIP-010 token whose `transfer` function deducts a fee (e.g., delivers `amount - fee` to the recipient while emitting success).
2. A user calls `deposit` with `amount = 1000`. `receive-underlying` invokes the token's `transfer`, but the vault contract's real balance only increases by `1000 - fee`.
3. `deposit` still executes `(var-set assets (+ current-assets amount))` with the full nominal `1000`, and mints shares computed from `1000` via `convert-to-shares-preview`, per `mainnet/contracts/vault/v0-vault-usdc.clar:779-783`.
4. Over repeated deposits, `assets` (and thus the share price) drifts above the vault's true token holdings.
5. A later redeemer calls `redeem`; `convert-to-assets-preview` returns an inflated `inkind` based on the inflated `assets` bookkeeping, and `send-underlying` attempts to pay it out — the last redeemers cannot be paid in full, and/or earlier redeemers extract more real value than they deposited, at the expense of the remaining depositors, insolvency in the vault's underlying pool.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L291-299)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L306-322)
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L761-793)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    (asserts! (<= (+ current-assets amount) CAP-SUPPLY) ERR-SUPPLY-CAP-EXCEEDED)

    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))

    (print {
      action: "deposit",
      caller: contract-caller,
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L795-810)
```text
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
```

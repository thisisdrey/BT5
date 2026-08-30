### Title
Directly-Transferred Underlying Tokens Are Permanently Unrecoverable in Vault Contracts - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and sibling vaults)

### Summary
The Zest v0 vault contracts (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`) track underlying-asset accounting entirely through an internal data-var, `assets`, rather than reconciling against the contract's real on-chain token balance. Any underlying tokens that reach the vault contract without going through `deposit` (e.g. a plain SIP-010/STX transfer, or tokens sent by mistake) never increment `assets`, are never reflected in share math, and can never be withdrawn — this mirrors the M-08 "donated tokens cannot be recovered" bug class, where funds sent to a contract outside its normal accounting path become permanently stuck because withdrawal logic only operates on tracked internal state, not actual balance.

### Finding Description
`total-assets` / `total-assets-preview` compute the vault's asset base purely from `(var-get assets)` plus accrued interest, with no reference to the actual token balance held by the contract: [1](#0-0) 

The contract does have a helper, `ubalance`, that reads the real balance via `contract-call? .wstx get-balance current-contract`, but it is dead code — never used in `deposit`, `redeem`, `total-assets`, `total-assets-preview`, or `get-available-assets`: [2](#0-1) 

`deposit` increments `assets` only by the `amount` argument passed by the caller, via `receive-underlying`, and mints shares based on `convert-to-shares-preview`, which itself is driven only by `total-assets-preview` (i.e., the tracked `assets` var): [3](#0-2) 

`redeem` burns shares and pays out `inkind`, computed from `convert-to-assets-preview`, then decrements the tracked `assets` var by that same `inkind` amount — again, entirely disconnected from the contract's real balance: [4](#0-3) 

Consequently, if any principal transfers the underlying token (wSTX, sBTC, stSTX, USDC, USDH, stSTXbtc) directly to the vault contract's address — outside `deposit` — the tokens land in the contract's real balance but the `assets` var is unaware of them. No `deposit`, `redeem`, or admin function reads `ubalance` or reconciles the discrepancy, so these tokens can never be minted as shares nor paid out by `redeem`. They are permanently stranded in the vault with no recovery path, identical in root cause and effect to the reported Shelter finding where donated tokens are separated from the contract's tracked accounting and become unclaimable once the accounting-only exit path (`exitShelter`) executes.

### Impact Explanation
Any underlying asset tokens accidentally or deliberately sent directly to a v0 vault contract (rather than through `deposit`) are permanently frozen — they cannot be withdrawn by any user, by the DAO, or reclaimed through any exposed function, since all vault logic (deposit, redeem, borrow, repay, socialize-debt, accrue) operates exclusively on the `assets` data-var and never syncs with the contract's actual token balance. This is a permanent freezing of funds at rest in the vault, matching the in-scope "permanent freezing of funds" impact class.

### Likelihood Explanation
Likelihood is moderate: this requires an unprivileged principal (or their own deployed contract) to send tokens directly to the vault's principal address instead of calling `deposit`, which can happen through user error, a misconfigured integration/bot, or a bridge/adapter contract that mistakenly `transfer`s the underlying asset to the vault address rather than invoking `deposit`. No malicious actor coordination, DAO compromise, or oracle manipulation is required — any ordinary SIP-010 `transfer` call targeting the vault's principal suffices to trigger the loss.

### Recommendation
Reconcile vault accounting against the real token balance rather than relying solely on the tracked `assets` var: e.g., have `deposit`/`accrue` compare `ubalance` (or the SIP-010/STX `get-balance` equivalent) against `(var-get assets)` and treat any positive difference as recoverable "excess" that can be swept to the DAO treasury (mirroring the report's suggested mitigation of allocating excess `amountInShelter` to the owner), or provide an explicit DAO-gated `sweep-excess` entry point that transfers the balance/assets differential to `.dao-treasury`.

### Proof of Concept
1. Any principal calls the underlying token's `transfer` function directly, sending `N` tokens to `v0-vault-stx`'s contract principal (bypassing `deposit`).
2. The vault's `ubalance` (real balance) increases by `N`, but `(var-get assets)` is untouched — confirmed by `receive-underlying`/`deposit` only calling `var-set assets (+ current-assets amount)` for amounts passed through `deposit` itself: [5](#0-4) 
3. `total-assets-preview`, `convert-to-shares-preview`, and `convert-to-assets-preview` all derive from `(var-get assets)`, never from `ubalance`, so the `N` tokens contribute zero shares and zero withdrawable balance: [6](#0-5) 
4. No subsequent call to `deposit`, `redeem`, `system-borrow`, `system-repay`, `accrue`, or `socialize-debt` reads or updates from `ubalance`, so the `N` tokens remain in the contract forever, unrecoverable by any user or the DAO.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L303-304)
```text
(define-private (ubalance)
  (unwrap-panic (contract-call? .wstx get-balance current-contract)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-346)
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

;; -- Debt helpers -----------------------------------------------------------

(define-private (total-debt)
  (calc-cumulative-debt (var-get principal-scaled) (var-get index)))

(define-private (debt-preview)
  (calc-cumulative-debt (var-get principal-scaled) (next-index)))

(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L765-797)
```text
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

(define-public (redeem (amount uint) (min-out uint) (recipient principal))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L799-833)
```text
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
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
  
  (print {
    action: "redeem",
    caller: contract-caller,
    data: {
      redeemer: account,
      recipient: recipient,
      shares-burned: amount,
      amount-received: inkind,
      assets: (- current-assets inkind)
    }
  })

  (ok inkind)))

;; -- Lending operations -----------------------------------------------------
```

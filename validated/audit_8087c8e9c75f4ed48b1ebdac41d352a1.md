### Title
Vault `deposit()` lacks a zero-shares-minted guard, allowing rounding to strand a depositor's underlying funds - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
Every Zest vault contract's `deposit()` function computes the shares to mint via `convert-to-shares-preview`, which can legitimately round down to `u0` when `total-supply > 0` and the deposited `amount` is small relative to `total-assets`. Unlike `redeem()`, which explicitly guards against a zero output with `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)`, `deposit()` has no equivalent check. This is the same root-cause pattern as the referenced report: a legitimate zero-amount rounding case is not special-cased, so the transfer/accounting proceeds as if a nonzero amount were exchanged, and the counter-party (here, the depositor) loses the value they provided.

### Finding Description
In `deposit()`: [1](#0-0) 

`inkind` (shares to mint) is derived from `convert-to-shares-preview`: [2](#0-1) 

```clarity
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))
```

When `ts > 0` (i.e., the vault already has depositors) and `ta` is large relative to `amount`, `mul-div-down amount ts ta` rounds down to `u0`. `deposit()` only checks:
```
(asserts! (>= inkind min-out) ERR-SLIPPAGE)
```
If the caller (or an integrator building on top, e.g. `supply-collateral-add` in the market contract) passes `min-out = 0` — which is the normal default for a "no slippage protection" call, as seen used throughout the test suite — this check passes trivially even when `inkind = u0`. The function then:
1. Pulls `amount` of underlying tokens from the caller via `receive-underlying`,
2. Mints `u0` zTokens to `recipient` via `(try! (ft-mint? zft inkind recipient))` (a no-op mint),
3. Increases `assets` by the full `amount`.

The depositor's underlying tokens are absorbed into vault `assets` (and correspondingly diluted to existing shareholders) while the depositor receives no zTokens whatsoever in return — an irrecoverable loss of their principal. This mirrors the reported bug class: an on-chain rounding-to-zero case is allowed by the math but not special-cased with a revert/guard, so real user-provided value is silently forfeited to other parties in the pool.

By contrast, `redeem()` explicitly protects against the analogous zero-output case: [3](#0-2) 

The same pattern (missing `ERR-OUTPUT-ZERO`-style check on `deposit`, present on `redeem`) appears identically across all vault instances: [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

### Impact Explanation
This falls under permanent loss/freezing of user funds at rest: the depositor irrevocably transfers underlying tokens into the vault and receives zero shares, meaning they have no claim on any vault assets in return. This is a direct, unconditional loss of principal for any unprivileged caller who deposits an amount that rounds to zero shares with `min-out` set to `0` (the common/default case, exactly as used in the market's `supply-collateral-add` composite flow and in test flows across the codebase).

### Likelihood Explanation
Likelihood is low-to-moderate and depends on vault share/asset ratio growing large enough (via yield accrual/treasury LP minting increasing `total-assets` relative to `total-supply`) that a plausible small deposit rounds `inkind` to zero. As the vault accrues interest and `total-assets-preview` grows, the share price rises, making rounding-to-zero increasingly likely for small deposits made with default `min-out = 0`, exactly the scenario the report describes for exercise payments.

### Recommendation
Add an explicit zero-shares guard in `deposit()`, mirroring the `ERR-OUTPUT-ZERO` check already present in `redeem()`:
```clarity
(asserts! (> inkind u0) ERR-OUTPUT-ZERO)
```
placed alongside the other preconditions in each `v0-vault-*.clar` `deposit()` function, so that a deposit whose rounded share output is zero reverts instead of silently absorbing the depositor's funds.

### Proof of Concept
1. Let the vault already have nonzero `total-supply` and `total-assets` such that the share price (`total-assets/total-supply`) is large (achieved over time via interest accrual and treasury-LP minting in `accrue`).
2. An unprivileged user calls `deposit(amount, min-out=0, recipient)` where `amount` is small enough that `mul-div-down amount ts ta == u0`.
3. `deposit()` passes all asserts (`amount > 0`; `inkind(=0) >= min-out(=0)`; cap check), pulls `amount` of underlying tokens from the user, mints `0` zTokens to `recipient`, and increases `assets` by `amount`.
4. The user has permanently lost `amount` of underlying tokens with no corresponding zToken shares to redeem them back.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L306-313)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L795-811)
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
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L765-793)
```text
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

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L761-793)
```text
;; -- Vault operations -------------------------------------------------------

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
```

**File:** mainnet/contracts/vault/v0-vault-ststxbtc.clar (L761-793)
```text
;; -- Vault operations -------------------------------------------------------

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
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L761-793)
```text
;; -- Vault operations -------------------------------------------------------

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
```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L761-793)
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

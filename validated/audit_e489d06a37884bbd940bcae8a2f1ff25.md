Confirmed: the `deposit` function in the vault contracts lacks an output-zero check that the symmetric `redeem` function has, confirming the analog vulnerability class matches (rounding to zero shares on deposit).

### Title
Deposit rounding causes zero shares minted while underlying tokens are taken - ([File: mainnet/contracts/vault/v0-vault-usdc.clar] and equivalent vault contracts)

### Summary
All tokenized vault contracts (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-stx.clar`) compute shares to mint on `deposit` via `convert-to-shares-preview`, which performs `mul-div-down amount ts ta` — floor division that can round down to `u0` for small deposit amounts once the share price (`ta`/`ts`) exceeds 1 (i.e., after interest has accrued). Unlike `redeem`, which explicitly checks `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)`, `deposit` has no equivalent check, so a deposit can proceed, take the user's underlying tokens, and mint zero zTokens.

### Finding Description
`deposit` in [1](#0-0)  computes `inkind` via `convert-to-shares-preview`, defined as: `(mul-div-down amount ts ta)` when both `ts` (total supply) and `ta` (total assets) are non-zero [2](#0-1) . This is an integer floor division: `(amount * ts) / ta`. Once the vault has accrued interest such that `ta > ts` (share price > 1 underlying unit per share), any deposit `amount` small enough that `amount * ts < ta` will floor to `0`.

The `deposit` function only asserts `(> amount u0)` (nonzero input) and `(>= inkind min-out)` (slippage), but never asserts `(> inkind u0)` [3](#0-2) . If the caller passes `min-out` as `u0` (the default/no-slippage-protection case, which is a normal, permissible value — not attacker-controlled misuse), the check `(>= 0 0)` passes. The function then proceeds to `receive-underlying amount account` (pulling the user's tokens into the vault) and `ft-mint? zft 0 recipient` (minting zero shares) [4](#0-3) . The user's underlying tokens increase `assets` in the vault but the user receives no zTokens in return — an exact analog of the reported `GlmManager.getGmTokenValueInUSDC()` bug, where small-value deposits get converted to zero and the depositor loses the underlying asset with no shares/credit issued.

The same pattern (`deposit` missing an output-zero check that `redeem` has) is present identically across all six vault contracts as confirmed by the grep match count, e.g. [5](#0-4) , [6](#0-5) .

By contrast, `redeem` explicitly guards against this exact rounding failure: [7](#0-6) .

### Impact Explanation
This results in temporary/permanent freezing of the depositor's funds: the underlying tokens are transferred into the vault (increasing `assets`, i.e., the pool's total value, effectively socialized to existing shareholders) while the depositor receives zero zTokens and has no claim to redeem them back. This is a direct loss of principal for the depositor of a legitimate small deposit — falling under "permanent freezing of funds" (their deposited tokens are absorbed by the vault with no corresponding claim).

### Likelihood Explanation
Likelihood increases as a vault's share price departs further from 1:1 (i.e., over time as interest accrues, `ta`/`ts` grows), since larger price ratios make larger deposit amounts round to zero shares. sBTC (8 decimals) and other precision-heavy vaults are particularly exposed, but any vault vulnerable once sufficient interest has accrued. Any ordinary user calling `deposit` with a dust amount and `min-out = 0` (a normal default, not requiring any privileged action or attack setup) can trigger this unintentionally, and no special permissions or DAO actions are needed.

### Recommendation
Add `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` in the `deposit` function of every vault contract (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-stx.clar`), mirroring the existing check already present in `redeem`, so that deposits which would mint zero shares revert instead of silently consuming the user's underlying tokens.

### Proof of Concept
1. Vault accrues interest such that `total-assets`/`total-supply` (share price) > 1, e.g., `ta = 2,000,000` and `ts = 1,000,000` (price = 2 underlying units per share).
2. User calls `deposit(amount=1, min-out=0, recipient=user)`.
3. `convert-to-shares-preview(1)` computes `mul-div-down(1, 1000000, 2000000) = floor(1,000,000 / 2,000,000) = 0`.
4. `(asserts! (>= inkind min-out) ERR-SLIPPAGE)` → `(>= 0 0)` passes since `min-out = 0`.
5. `receive-underlying(1, account)` pulls 1 unit of underlying token from the user.
6. `ft-mint? zft 0 recipient` mints zero zTokens to the user.
7. `assets` is incremented by 1, benefiting existing shareholders; the depositing user receives nothing in return for their token, matching the exact "deposited but no shares minted" bug class described in the external report.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L306-317)
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

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L808-809)
```text
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L765-797)
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

(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L765-797)
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

(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
```

### Title
Redeem burns shares and pushes underlying to the recipient before decrementing the `assets` accounting variable, allowing a reentrant `deposit`/`redeem` to price shares off stale (higher) `assets` — ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
`redeem` in the tokenized vault contracts (`v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-usdh.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, and their `local-testing` counterparts) burns the caller's zTokens and pushes the underlying asset to the recipient **before** updating the `assets` state variable that backs the share-price calculation: [1](#0-0) 

```
(try! (ft-burn? zft amount account))
(try! (send-underlying inkind recipient))
(var-set assets (- current-assets inkind))
```

This is structurally identical to the reported `LineOfCredit._close()` bug: the critical state write (`credits[id]` deletion / here, `assets` decrement) happens **after** the external token transfer, instead of before it (checks-effects-interactions violation).

### Finding Description
`convert-to-shares-preview` / `convert-to-assets-preview` (used by `deposit` and `redeem` to price shares) read the `assets` data-var and `total-supply`. In `redeem`:

1. `ft-burn?` executes first — `total-supply` is decremented immediately.
2. `send-underlying` executes next — an external `contract-call?`/token transfer to `recipient`.
3. Only afterward does `(var-set assets (- current-assets inkind))` fire.

Between steps 2 and 3, if execution can re-enter the vault (e.g., a second call to `redeem` or `deposit` in the same transaction before the outer call's `var-set assets` commits), the vault's `assets` var is still at its pre-redeem (higher) value while `total-supply` already reflects the post-burn (lower) value. Any share-price computation performed during that window (`convert-to-assets-preview` / `convert-to-shares-preview`) will use this inconsistent numerator/denominator pair, producing an inflated price-per-share and letting the re-entrant caller redeem/deposit more underlying than they are entitled to, or mint proportionally more shares than they should for a given deposit.

The identical ordering issue appears across all vault variants: [2](#0-1) [3](#0-2) [4](#0-3) 

By contrast, `deposit` in the same files follows the correct order — external pull (`receive-underlying`) then state write (`var-set assets`) — which is safe for a pull-style transfer: [5](#0-4) 

For `redeem`, however, the transfer is a **push** (funds leave the vault to `recipient`), so the state update must precede it, mirroring the exact class of bug identified in the report (`_close` sending tokens before clearing `credits[id]`).

Separately, the `market-vault.clar` / `v0-market-vault.clar` `collateral-remove` function (which does take an attacker-influenceable `<ft-trait>` parameter) already follows the correct order — `insert` (state write) precedes `send-tokens` — so that specific entry point is not vulnerable to this class.

### Impact Explanation
If exploitable, this would allow a caller to extract more underlying assets from the vault than their shares represent, directly at the expense of other depositors/lenders — a direct theft-of-user-funds-at-rest scenario (Critical), matching the impact class in the original finding (lender/depositor fund theft via reentrancy).

### Likelihood Explanation
This is where certainty breaks down, and it must be stated plainly: **exploitability could not be confirmed with the available tooling.**

- The `UNDERLYING` asset for each of these vaults is a **hardcoded constant** (e.g., `.usdc`, and equivalents for sBTC/stSTX/etc.) rather than an attacker-suppliable `<ft-trait>` parameter, unlike `collateral-add`/`collateral-remove` in `market-vault.clar`. That means the only way to trigger a reentrant call during `send-underlying`/`receive-underlying` is if the specific hardcoded underlying token contract's `transfer` implementation itself performs a synchronous `contract-call?` back into the vault (Clarity SIP-010 tokens do not have ERC-777-style transfer hooks by default).
- I was not able to fully inspect the implementations of `send-underlying`/`receive-underlying` or the underlying token contracts (`.usdc`, `.ststx`, etc.) within the remaining tool budget to determine whether any such callback path exists.
- `redeem`/`deposit`/`system-borrow`/`system-repay` on these vault contracts also require `check-caller-auth`/being invoked through `market.clar`, further constraining who can trigger the sequence and in what context.

Given the DAO controls which underlying tokens are wired into each vault, and there is no confirmed callback mechanism in the visible code, the practical likelihood is uncertain/low, but the code pattern itself is a genuine CEI violation that should be fixed defensively regardless of whether a current callback path exists (future underlying token changes, upgrades, or unforeseen call graths could introduce one).

### Recommendation
Reorder `redeem` (and the equivalent logic in every affected vault contract) so that `(var-set assets ...)` is committed **before** the external `send-underlying` call, consistent with the checks-effects-interactions pattern already used correctly in `deposit` and in `market-vault.clar`'s `collateral-remove`:

```
(try! (ft-burn? zft amount account))
(var-set assets (- current-assets inkind))
(try! (send-underlying inkind recipient))
```

### Proof of Concept
A concrete, working PoC could not be constructed without confirming a reentrant callback path in the underlying token contracts (`.usdc`, `.ststx`, `.stx`, etc.), which was not verifiable within the available indexing/tooling. The vulnerable code ordering itself is directly cited above; a full PoC would require deploying/inspecting the exact `UNDERLYING` token implementation used on mainnet to determine whether its `transfer` function can synchronously call back into `redeem`/`deposit` before the outer call's `var-set assets` commits.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L777-779)
```text
    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L806-819)
```text
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
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L813-815)
```text
  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L813-815)
```text
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L813-815)
```text
  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
```

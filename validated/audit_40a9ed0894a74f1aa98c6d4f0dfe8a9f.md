### Title
Vault `initialize()` can be front-run/griefed by any unprivileged caller, blocking the DAO's atomic protocol-init proposal - ([File: mainnet/contracts/vault/v0-vault-sbtc.clar])

### Summary
`initialize()` in every `v0-vault-*` contract only checks `(not (var-get initialized))` and contains no `check-dao-auth` gate, unlike essentially every other state-mutating admin function in the same contracts (`set-cap-supply`, `set-fee-flash`, `set-fee-reserve`, etc., all of which call `try! (check-dao-auth)`).

### Finding Description
`initialize()` is a `define-public` function with no caller restriction:

```
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    ...
    (ok true)))
``` [1](#0-0) 

This is the same code, with the same missing auth check, across `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, and `v0-vault-ststxbtc.clar`. [2](#0-1) [3](#0-2) [4](#0-3) 

The intended flow is that the DAO executes `proposal-protocol-init.clar` / `proposal-init-vaults.clar` in one atomic transaction: it sets vault caps, then calls `initialize` on every vault as part of a single `try!` chain wrapped in `as-contract? ((with-all-assets-unsafe))`, then authorizes the market contract, then sets up egroups:

```
(try! (as-contract? ((with-all-assets-unsafe))
  (try! (contract-call? .vault-sbtc initialize))
  (try! (contract-call? .vault-usdh initialize))
  (try! (contract-call? .vault-usdc initialize))
  (try! (contract-call? .vault-ststx initialize))
  (try! (contract-call? .vault-stx initialize))
))
``` [5](#0-4) 

Because `initialize()` is unprivileged and idempotency is enforced only by the `initialized` boolean, any ordinary account can call `vault-sbtc.initialize()` (or any other vault's `initialize`) directly — supplying the `MINIMUM-LIQUIDITY` amount of the underlying token from their own balance via the underlying `deposit` call — before the DAO's proposal transaction executes. This flips `initialized` to `true` ahead of the DAO. When the DAO's proposal subsequently runs its multi-step `try!` chain and reaches `(contract-call? .vault-sbtc initialize)`, that call reverts with `ERR-ALREADY-INITIALIZED`, which aborts the entire proposal transaction (all `try!`s in a Clarity transaction roll back together). This is analogous to the reported issue: an unprivileged party performs a "pool/pair creation"-equivalent action (here, vault share-pool initialization) ahead of the protocol's own privileged setup call, using the exact same "no one has done this yet" precondition (`getPair == address(0)` vs. `not initialized`), causing the protocol's atomic setup transaction to be denied.

### Impact Explanation
This is a temporary freezing / denial-of-availability of the protocol's launch/upgrade flow rather than a direct theft of funds: the DAO's atomic multi-vault initialization (caps, initialize, market authorization, egroup creation in `proposal-protocol-init.clar`) is a single transaction; if any one `initialize()` sub-call fails, the whole proposal reverts, so no vault gets authorized, no egroups get created, and the market cannot go live as planned. The DAO must then discover the griefed state and construct a new, non-atomic proposal that skips the already-initialized vault(s), delaying protocol launch and re-auditing the modified init sequence. This matches the in-scope "temporary freezing of funds/protocol availability" impact class (funds cannot be deposited/borrowed against the market until DAO redeploys a corrected proposal), while no user funds are put at direct risk (the attacker only spends real underlying tokens to the vault, receiving no shares back since `deposit` mints to `NULL-ADDRESS`).

### Likelihood Explanation
The precondition (`initialized == false`) and the exact function signature (`initialize`, taking no auth-gated arguments) are public and visible in the deployed/deployable bytecode/source before the DAO proposal executes. Any address holding a small amount of the relevant underlying token (e.g., sBTC, USDC, USDH, wstx, ststx) can call `initialize()` directly at any time prior to the DAO's proposal transaction; this requires no privileged access, no flashloan, and no oracle manipulation — only front-running/mempool-watching the DAO's known upcoming initialization transaction, or simply calling it opportunistically at any point before the DAO does. This is a straightforward, cheap, unprivileged griefing vector.

### Recommendation
Gate `initialize()` behind `check-dao-auth` (the same pattern used by every other administrative function in the vault, e.g. `set-cap-supply`, `set-fee-reserve`, `set-authorized-contract`), so only the DAO executor can call it. This removes the ability for an arbitrary caller to pre-empt the DAO's atomic initialization sequence.

### Proof of Concept
1. Deploy vaults per `v0-vault-sbtc.clar` (or any of the six `v0-vault-*` contracts) with `initialized` at its default `false` [6](#0-5) .
2. Before the DAO executes `proposal-protocol-init.clar` (or `proposal-init-vaults.clar`), an arbitrary account with a small sBTC balance calls `vault-sbtc.initialize()` directly. This succeeds because there is no auth check, only the `initialized` guard [1](#0-0) ; `initialized` is now `true`.
3. The DAO then submits/executes `proposal-protocol-init.clar`, which reaches `(try! (contract-call? .vault-sbtc initialize))` inside its `try!` chain [7](#0-6) . This sub-call now returns `ERR-ALREADY-INITIALIZED`, which propagates through `try!` and aborts the entire proposal execution — no caps get set beyond that point, no market authorization happens, and no egroups are created in that transaction.
4. The DAO must construct and pass a new proposal that conditionally skips the already-initialized vault(s), delaying the protocol launch/upgrade.

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L75-76)
```text
;; -- Initialization state
(define-data-var initialized bool false)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L492-507)
```text
(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
    
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L492-507)
```text
;; -- Initialization ---------------------------------------------------------

(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L490-507)
```text
;; -- Initialization ---------------------------------------------------------

(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
    
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L490-507)
```text
;; -- Initialization ---------------------------------------------------------

(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
    
    (ok true)))
```

**File:** local-testing/contracts/proposals/proposal-init-vaults.clar (L26-33)
```text
    ;; Called directly - dao-executor's with-all-assets-unsafe handles asset transfers
    (try! (as-contract? ((with-all-assets-unsafe))
      (try! (contract-call? .vault-sbtc initialize))
      (try! (contract-call? .vault-usdh initialize))
      (try! (contract-call? .vault-usdc initialize))
      (try! (contract-call? .vault-ststx initialize))
      (try! (contract-call? .vault-stx initialize))
    ))
```

### Title
Front-runnable, unauthenticated vault `initialize()` allows griefing/DoS of protocol deployment - (File: `mainnet/contracts/vault/v0-vault-sbtc.clar` and analogous `v0-vault-*.clar` files)

### Summary
Each vault contract's `initialize` function has no caller/access-control check — any unprivileged principal can call it before the DAO's deployment proposal does, permanently blocking the intended DAO-driven initialization flow.

### Finding Description
The `initialize` function in the vault contracts only guards against double-initialization, not against who calls it: [1](#0-0) 

There is no `check-dao-auth` or similar caller restriction, unlike the sibling function `set-authorized-contract`, which explicitly calls `check-dao-auth`: [2](#0-1) 

The intended deployment flow is for the DAO's initialization proposal (e.g. `proposal-protocol-init.clar` / `v0-init.clar`) to call `initialize` on every vault as part of a single atomic multi-step setup transaction, alongside setting caps and authorizing the market contract: [3](#0-2) 

Because `initialize` has no access-control check, and because the vault contract itself is deployed on-chain (publicly visible in the mempool/blockchain) before the DAO executes its initialization proposal in a later transaction, any unprivileged principal can call `initialize` directly on the freshly-deployed vault. This mirrors the exact bug class in the referenced report: initializer functions lack a check for who is calling them, and an attacker who observes the deployment sequence can insert their own initializing transaction ahead of the legitimate one.

### Impact Explanation
Since `initialize` uses `asserts! (not (var-get initialized))` and is idempotent-guarded (cannot be re-run), once an attacker front-runs it, the DAO's own subsequent call to `initialize` inside its atomic proposal (`try! (contract-call? .vault-X initialize))`) will fail with `ERR-ALREADY-INITIALIZED`, which aborts the entire proposal transaction due to the `try!` wrapper. This causes the whole protocol-initialization proposal to revert, requiring a fresh deployment/redeployment attempt — a protocol failure / deployment DoS impact, consistent with "Failure of the protocol, with the need for redeploy" from the referenced report. This falls under the in-scope "temporary freezing of funds" impact class, since caps, authorizations, and egroup setup performed atomically in the same proposal cannot complete, delaying the vault's availability to depositors/borrowers until the DAO detects and redeploys/adjusts the script.

Note that the attacker does not personally profit (the `MINIMUM-LIQUIDITY` they deposit via `initialize` is sent to `NULL-ADDRESS` and is unrecoverable by anyone, including the attacker), so this is a griefing/DoS vector rather than a direct theft vector.

### Likelihood Explanation
Likelihood is Medium: it requires an attacker to actively monitor the mempool/chain for the vault contract deployment and race a transaction between contract publish and the DAO's initialization proposal. On Stacks, block-based finality and the DAO's use of a single atomic proposal transaction narrow, but do not eliminate, the window; any block boundary between vault deployment and proposal execution is an opportunity.

### Recommendation
Restrict `initialize` to only be callable by the DAO (or the deploying/executor principal), analogous to the `check-dao-auth` guard already used in `set-authorized-contract`, `set-flashloan-permissions`, `set-cap-supply`, etc. Alternatively, redesign deployment so vault initialization occurs atomically as part of contract deployment rather than as a separate, later-called public function.

### Proof of Concept
1. DAO/deployer publishes `vault-sbtc.clar` (or any `v0-vault-*` contract).
2. Before the DAO's `proposal-protocol-init` (or `proposal-init-vaults`) transaction is mined, an attacker observes the vault contract address on-chain and submits their own `(contract-call? .vault-sbtc initialize)` transaction with a minimal amount to cover `MINIMUM-LIQUIDITY`.
3. Attacker's transaction confirms first; `var-get initialized` becomes `true`, and `MINIMUM-LIQUIDITY` shares are minted to `NULL-ADDRESS` from the attacker's deposited assets.
4. When the DAO's initialization proposal executes, `(try! (contract-call? .vault-sbtc initialize))` returns `ERR-ALREADY-INITIALIZED`, causing the entire multi-step proposal transaction (cap-setting, market authorization, egroup creation) to revert.
5. Protocol deployment must be manually detected, patched, and redeployed to proceed. [4](#0-3)

### Citations

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L490-507)
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

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L509-525)
```text
;; -- Auth management --------------------------------------------------------

(define-public (set-authorized-contract (contract principal) (authorized bool))
  (begin
    (try! (check-dao-auth))
    
    (print {
      action: "vault-set-authorized-contract",
      caller: tx-sender,
      data: {
        vault: UNDERLYING,
        contract: contract,
        authorized: authorized
      }
    })
    
    (ok (map-set authorized-contracts contract authorized))))
```

**File:** local-testing/contracts/proposals/mainnet/proposal-protocol-init.clar (L159-184)
```text
    ;; STEP 2: INITIALIZE MARKET-VAULT
    (try! (contract-call? .market-vault set-impl .market))
    
    ;; STEP 3: CONFIGURE VAULTS
    
    ;; Set vault caps
    (try! (contract-call? .vault-stx set-cap-supply CAP-STX-SUPPLY))
    (try! (contract-call? .vault-stx set-cap-debt CAP-STX-DEBT))
    (try! (contract-call? .vault-sbtc set-cap-supply CAP-SBTC-SUPPLY))
    (try! (contract-call? .vault-sbtc set-cap-debt CAP-SBTC-DEBT))
    (try! (contract-call? .vault-ststx set-cap-supply CAP-STSTX-SUPPLY))
    (try! (contract-call? .vault-ststx set-cap-debt CAP-STSTX-DEBT))
    (try! (contract-call? .vault-usdc set-cap-supply CAP-USDC-SUPPLY))
    (try! (contract-call? .vault-usdc set-cap-debt CAP-USDC-DEBT))
    (try! (contract-call? .vault-usdh set-cap-supply CAP-USDH-SUPPLY))
    (try! (contract-call? .vault-usdh set-cap-debt CAP-USDH-DEBT))
    (try! (contract-call? .vault-ststxbtc set-cap-supply CAP-STSTXBTC-SUPPLY))
    (try! (contract-call? .vault-ststxbtc set-cap-debt CAP-STSTXBTC-DEBT))
    
    ;; Authorize market contract in all vaults
    (try! (contract-call? .vault-stx set-authorized-contract .market true))
    (try! (contract-call? .vault-sbtc set-authorized-contract .market true))
    (try! (contract-call? .vault-ststx set-authorized-contract .market true))
    (try! (contract-call? .vault-usdc set-authorized-contract .market true))
    (try! (contract-call? .vault-usdh set-authorized-contract .market true))
    (try! (contract-call? .vault-ststxbtc set-authorized-contract .market true))
```

### Title
`system-repay` in vault contracts can be permanently blocked by an underlying token that reverts on zero-value transfers - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`, `v0-vault-sbtc.clar`, `v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`)

### Summary
`system-repay` computes `capped-amount` as `min(amount, debt)` and unconditionally forwards `capped-amount` to `receive-underlying`, which performs a token `transfer` call. If `debt` is already `0` when `system-repay` is invoked with a non-zero `amount`, `capped-amount` collapses to `0`, and the contract still issues a `0`-value token transfer. Any underlying asset that reverts on zero-value transfers (the same weird-ERC20/SIP-010 class referenced in the report) would cause this call, and therefore the whole repay flow reachable from the public market entry point, to permanently revert for that borrower/vault.

### Finding Description
`system-repay` is defined identically across the vault contracts, e.g.: [1](#0-0) 

```
(define-public (system-repay (amount uint))
  (let (
        ...
        (debt (total-debt))
        (capped-amount (if (> amount debt) debt amount))
        ...
        )
    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-underlying capped-amount tx-sender))
    ...
```

The only zero-guard present is `(asserts! (> amount u0) ...)`, which checks the caller-supplied `amount`, not the actually-transferred `capped-amount`. When `debt` is `0` (e.g., the position was already fully repaid by a preceding transaction, or a liquidation/other repay closed the debt first) and a caller still submits `amount > 0`, `capped-amount` becomes `0`. `receive-underlying` is then called with this `0` value: [2](#0-1) 

```
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount account current-contract none))
    (ok true)))
```

This issues a live `transfer` call of `0` tokens on the underlying SIP-010 asset contract. This is the exact bug class in the report: some fungible-token implementations revert on a zero-amount transfer. Unlike `PublicVault#transferWithdrawReserve` in the referenced report, which is reachable by any address with no access control, here `system-repay` is gated by `check-caller-auth`, restricting the direct caller to the authorized market contract. It is reachable indirectly by an ordinary principal, since `v0-4-market.clar`'s public `repay` entry point (used by any user) forwards into `system-repay` on the corresponding vault:



An ordinary borrower repaying an already-repaid or already-liquidated position (a natural race: two repay transactions in the same block, or a repay racing a liquidation that fully closes the debt) triggers the zero-`capped-amount` path.

### Impact Explanation
If the underlying asset used by a given vault (e.g. `usdc`, `sbtc`, `usdh`, `stx`, `ststx`) reverts on a zero-value transfer, every subsequent `repay` call routed through the market to that vault's `system-repay` for a position with zero remaining debt would revert. Since `system-repay` is the sole entry point used to reduce `principal-scaled`/`total-borrowed` state, this can create a scenario where a legitimate repay transaction (front-run or naturally racing another repay/liquidation) permanently fails, and — depending on how the market's repay flow composes calls — can block borrowers or liquidators from completing otherwise-valid operations. This lands in the "temporary freezing of funds" bucket (unclaimed collateral/repayment flows blocked) rather than direct theft, matching the High impact class for temporary freezing of funds.

### Likelihood Explanation
Likelihood depends on whether the specific SIP-010 token contract wrapped as `usdcx`/`sbtc-token`/`usdh-token-v1`/`ststx-token`/`wstx` actually reverts on a zero-amount `transfer`. This is not verified in-repo (these are external token contracts) — same caveat as the original report, which itself only cites this as a known weird-token behavior class rather than proven against a specific asset. The triggering condition itself (submitting `amount > 0` to `system-repay` while `debt` is already `0`) is easily and routinely reachable via ordinary repay-race conditions, so if any of the six wrapped underlying assets exhibits this token behavior, the finding is directly exploitable without special privilege.

### Recommendation
Guard the transfer with a zero-amount check before calling `receive-underlying`, e.g. only call `receive-underlying` when `capped-amount > 0`, and skip/no-op (or return `(ok true)`) when `capped-amount` is `0`, mirroring the same fix pattern recommended in the referenced report for `transferWithdrawReserve`. Apply this fix consistently in all six `v0-vault-*.clar` contracts.

### Proof of Concept
1. Borrower B has an open debt position (`debt > 0`) in `v0-vault-usdc.clar`.
2. Two repay transactions for the full remaining debt are submitted; the first one lands, calling `system-repay` and reducing `total-borrowed`/`principal-scaled` to `0` (`debt` now `0`).
3. The second repay transaction (already pending, or resubmitted with `amount > 0`) reaches `system-repay`; `capped-amount = (if (> amount debt) debt amount)` evaluates to `0` because `debt` is now `0`.
4. `(try! (receive-underlying capped-amount tx-sender))` executes a `transfer` of `0` USDC.
5. If the underlying `usdcx` token reverts on zero-value transfers, this call reverts, and the entire `system-repay`/market `repay` transaction fails — with no fallback path, indefinitely blocking that repay attempt for this asset until amount tracking is fixed off-chain by the caller. [1](#0-0) [2](#0-1)

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L291-296)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amount uint) (account principal))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L900-923)
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

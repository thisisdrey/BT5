I was unable to fully verify this due to file size limits. `mainnet/contracts/market/v0-4-market.clar` is 1661 lines and the tool truncated output at line 1000, so I could not read the actual `liquidate-redeem` function body, the `borrow` function's `last-borrow-block` update logic, or the exact location of the `ERR-LIQUIDATION-BORROW-SAME-BLOCK` assertion within `liquidate` vs. `liquidate-redeem`.

What I could confirm from the visible portion of the file:
- `ERR-LIQUIDATION-BORROW-SAME-BLOCK` is defined as a constant [1](#0-0) .
- There is a single shared private helper, `get-liquidation-position`, explicitly documented as "liquidation specific (enabled collateral + all debt)", which reads the position (including `last-borrow-block`) via `.v0-market-vault get-position` [2](#0-1) .
- The position structure returned includes a `last-borrow-block` field used in notional evaluation [3](#0-2) .

This structurally supports the premise that any liquidation entry point calling `get-liquidation-position` would read identical `last-borrow-block` data, since it is a single shared private function rather than duplicated logic per entry point. However, I could not locate and read the actual `liquidate` and `liquidate-redeem` public function definitions (they are past line 1000, in the truncated portion) to confirm:
1. Whether the `ERR-LIQUIDATION-BORROW-SAME-BLOCK` check is actually present in `liquidate-redeem`, and at what point in the call flow.
2. Whether `liquidate-redeem` has any additional/independent mitigation not present in `liquidate` (e.g., different ordering, different receiver validation, or a separate freshness check).
3. How `last-borrow-block` is set in the `borrow` function, and whether the dust-borrow-defeat mechanism described in the premise is accurate as stated.

Given the audit rules require "exact file/function support and a reproducible Clarinet/vitest simnet PoC," I cannot responsibly confirm or produce a full finding without seeing the actual `liquidate-redeem` function body and the `borrow` function's write to `last-borrow-block`.

Due to index size limits, some file contents (specifically the `liquidate`, `liquidate-redeem`, and `borrow` function bodies past line 1000 of `v0-4-market.clar`) were not available to me. I'd recommend starting a Devin session to pull the full file contents and confirm the exact guard placement in both liquidation entry points before treating this as a confirmed, reportable finding.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L90-90)
```text
(define-constant ERR-LIQUIDATION-BORROW-SAME-BLOCK (err u400024))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L473-475)
```text
(define-private (get-liquidation-position (account principal)) ;; liquidation specific (enabled collateral + all debt)
  (let ((mask (get-enabled-bitmap)))
    (contract-call? .v0-market-vault get-position account mask)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L516-524)
```text
        position: {
          id: uint,
          account: principal,
          mask: uint,
          last-update: uint,
          last-borrow-block: uint,
          collateral: (list 64 { aid: uint, amount: uint }),
          debt: (list 64 { aid: uint, scaled: uint }),
        },
```

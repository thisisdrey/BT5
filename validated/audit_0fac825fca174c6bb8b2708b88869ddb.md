### Title
Disabled-API allowlist can be bypassed on PBFT/Solidity HTTP endpoints whose URL structure doesn't match the parser's fixed-depth assumption - (File: framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java)

### Summary
`HttpApiAccessFilter.isDisabled()` decides whether an HTTP API is blocked by taking the request's normalized path and indexing `endpoint.split("/")[2]` to extract the "API name" to compare (case-insensitively) against `disabledApi`. This parsing hard-codes the assumption that every routed endpoint has the shape `/<contextOrPrefix>/<apiName>` (e.g. `/wallet/getaccount`), matching the way `FullNodeHttpApiService` and `SolidityNodeHttpApiService` register servlets. It does not account for the alternate, "abbreviated" routing form used elsewhere in the code base, where the same API is exposed at a shallower path.

### Finding Description
`isDisabled` is:
```java
endpoint = URI.create(endpoint).normalize().toString();
List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
if (!disabledApiList.isEmpty()) {
  disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
}
``` [1](#0-0) 

This assumes `endpoint` always has at least 3 `/`-delimited components (`["", "wallet"/"walletsolidity", "<api>"]`). `HttpApiOnPBFTService.addServlet` (the PBFT HTTP surface, which reuses `HttpApiAccessFilter` mapped to `"/*"`) registers many of the exact same operator-configurable APIs at *root-level, single-segment* paths, e.g. `"/getaccount"`, `"/getnowblock"`, `"/getblockbynum"`, `"/triggerconstantcontract"`, `"/getBrokerage"`, `"/getReward"`, etc. [2](#0-1) 

For such a request, `endpoint` normalizes to `"/getaccount"`, so `endpoint.split("/")` yields `["", "getaccount"]` — a 2-element array. Indexing `[2]` throws `ArrayIndexOutOfBoundsException`, which is swallowed by the surrounding `catch (Exception e)` block, leaving `disabled` at its default value `false`. [1](#0-0) 

The filter is wired into the PBFT service via `addFilter`, using the same `httpApiAccessFilter` bean and relying on the exact `isDisabled` parser: [3](#0-2) 

The existing regression test (`HttpApiAccessFilterTest.testHttpFilter` / `testIsDisabled`) only exercises URLs shaped like `/wallet/<api>`, `/walletsolidity/<api>`, `/walletpbft/<api>` and never a bare `/<api>` path, so this parsing gap was never caught: [4](#0-3) [5](#0-4) 

This is directly analogous to the reported bug class: a security allowlist/denylist parser recognizes only one canonical shape of the target (here, `/wallet/<api>`-style paths) and silently fails open (defaulting to "not disabled") when the same logical API is reached through an alternate routing form/alias (the PBFT flat-path registration), rather than rejecting or safely handling the unrecognized shape.

### Impact Explanation
An operator who disables a sensitive HTTP API (e.g. `getaccount`, `triggerconstantcontract`, `getnowblock`) via `disabledApi` in config, intending to close it off across all node HTTP interfaces (FullNode/Solidity/PBFT), still exposes it on the PBFT HTTP interface, because the same API name reachable at PBFT's root-relative path silently bypasses the block due to the swallowed exception. Depending on which API is disabled, this can re-expose account/contract query, or read/estimate/trigger endpoints that were deliberately disabled for security, information-disclosure, or DoS-mitigation reasons — i.e., an anonymous HTTP client can reach an API the node operator explicitly intended to no longer serve.

### Likelihood Explanation
High reachability: no authentication is required to hit these HTTP endpoints; an anonymous client only needs network access to the PBFT HTTP port and knowledge that the operator disabled a given API name via config. The bypass is deterministic (any single-segment-path request to that filter triggers the exception every time), requiring no race condition or special privilege.

### Recommendation
Rewrite `isDisabled` to not depend on positional array indexing of a path assumed to have a fixed depth. Extract the last path segment (or the terminal segment of the *servlet path*, not the full `contextPath + servletPath`) robustly, e.g. via `Paths.get(endpoint).getFileName()` or a regex like `.*/([^/]+)$`, and treat any endpoint whose extracted segment matches (case-insensitively) an entry in `disabledApiList` as disabled — regardless of how many leading path segments (`/wallet`, `/walletsolidity`, or none) precede it. Additionally, change the failure mode: if endpoint parsing throws an unexpected exception, prefer a fail-closed default appropriate to a security-relevant allowlist (or explicitly enumerate and unit-test every registered route shape, including root-level PBFT registrations) instead of silently defaulting to "not disabled."

### Proof of Concept
1. Configure the node with PBFT HTTP enabled and `disabledApi = ["getaccount"]` in `config.conf`.
2. Confirm the block works for the FullNode/Solidity path form:
   `curl http://<node>:<httpPort>/wallet/getaccount` → returns `{"Error":"this API is unavailable due to config"}` (as validated by the existing test `HttpApiAccessFilterTest.testHttpFilter`).
3. Send the same disabled API request against the PBFT HTTP port using the root-relative path that `HttpApiOnPBFTService` actually registers:
   `curl http://<node>:<pbftHttpPort>/getaccount`
4. Because `endpoint` normalizes to `/getaccount`, `endpoint.split("/")` has only 2 elements, `[2]` throws `ArrayIndexOutOfBoundsException`, the catch block logs a warning and leaves `disabled = false`, and the request is forwarded to `accountOnPBFTServlet` normally — returning account data instead of the expected "API is unavailable" error, confirming the allowlist bypass.

### Citations

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L60-74)
```java
  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      endpoint = URI.create(endpoint).normalize().toString();
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.warn("check isDisabled except, endpoint={}, {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnPBFT/http/PBFT/HttpApiOnPBFTService.java (L179-227)
```java
  @Override
  protected void addServlet(ServletContextHandler context) {
    // same as FullNode
    context.addServlet(new ServletHolder(accountOnPBFTServlet), "/getaccount");
    context.addServlet(new ServletHolder(listWitnessesOnPBFTServlet), "/listwitnesses");
    context.addServlet(new ServletHolder(getAssetIssueListOnPBFTServlet), "/getassetissuelist");
    context.addServlet(new ServletHolder(getPaginatedAssetIssueListOnPBFTServlet),
        "/getpaginatedassetissuelist");
    context
        .addServlet(new ServletHolder(getAssetIssueByNameOnPBFTServlet), "/getassetissuebyname");
    context.addServlet(new ServletHolder(getAssetIssueByIdOnPBFTServlet), "/getassetissuebyid");
    context.addServlet(new ServletHolder(getAssetIssueListByNameOnPBFTServlet),
        "/getassetissuelistbyname");
    context.addServlet(new ServletHolder(getNowBlockOnPBFTServlet), "/getnowblock");
    context.addServlet(new ServletHolder(getBlockByNumOnPBFTServlet), "/getblockbynum");
    context.addServlet(new ServletHolder(getDelegatedResourceOnPBFTServlet),
        "/getdelegatedresource");
    context.addServlet(new ServletHolder(getDelegatedResourceAccountIndexOnPBFTServlet),
        "/getdelegatedresourceaccountindex");
    context.addServlet(new ServletHolder(getExchangeByIdOnPBFTServlet), "/getexchangebyid");
    context.addServlet(new ServletHolder(listExchangesOnPBFTServlet), "/listexchanges");
    context.addServlet(new ServletHolder(getAccountByIdOnPBFTServlet), "/getaccountbyid");
    context.addServlet(new ServletHolder(getBlockByIdOnPBFTServlet), "/getblockbyid");
    context
        .addServlet(new ServletHolder(getBlockByLimitNextOnPBFTServlet), "/getblockbylimitnext");
    context
        .addServlet(new ServletHolder(getBlockByLatestNumOnPBFTServlet), "/getblockbylatestnum");
    context.addServlet(new ServletHolder(getMerkleTreeVoucherInfoOnPBFTServlet),
        "/getmerkletreevoucherinfo");
    context.addServlet(new ServletHolder(scanAndMarkNoteByIvkOnPBFTServlet),
        "/scanandmarknotebyivk");
    context.addServlet(new ServletHolder(scanNoteByIvkOnPBFTServlet), "/scannotebyivk");
    context.addServlet(new ServletHolder(scanNoteByOvkOnPBFTServlet), "/scannotebyovk");
    context.addServlet(new ServletHolder(isSpendOnPBFTServlet), "/isspend");
    context.addServlet(new ServletHolder(triggerConstantContractOnPBFTServlet),
        "/triggerconstantcontract");
    context.addServlet(new ServletHolder(estimateEnergyOnPBFTServlet), "/estimateenergy");

    // only for PBFTNode
    context.addServlet(new ServletHolder(getTransactionByIdOnPBFTServlet), "/gettransactionbyid");
    context.addServlet(new ServletHolder(getTransactionInfoByIdOnPBFTServlet),
        "/gettransactioninfobyid");

    context.addServlet(new ServletHolder(getTransactionCountByBlockNumOnPBFTServlet),
        "/gettransactioncountbyblocknum");

    context.addServlet(new ServletHolder(getNodeInfoOnPBFTServlet), "/getnodeinfo");
    context.addServlet(new ServletHolder(getBrokerageServlet), "/getBrokerage");
    context.addServlet(new ServletHolder(getRewardServlet), "/getReward");
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnPBFT/http/PBFT/HttpApiOnPBFTService.java (L267-278)
```java
  @Override
  protected void addFilter(ServletContextHandler context) {
    // filters the specified APIs
    // when node is lite fullnode and openHistoryQueryWhenLiteFN is false
    context.addFilter(new FilterHolder(liteFnQueryHttpFilter), "/*",
        EnumSet.allOf(DispatcherType.class));

    // api access filter
    context.addFilter(new FilterHolder(httpApiAccessFilter), "/*",
        EnumSet.allOf(DispatcherType.class));
  }
}
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L60-90)
```java
    List<String> patterns = new ArrayList<>();
    patterns.add("/walletsolidity/");
    patterns.add("/walletpbft/");
    patterns.add("/wallet/");

    int httpPort;
    String ip = "127.0.0.1";
    for (String api : disabledApiList) {
      for (String pattern : patterns) {
        String urlPath = pattern + api;
        if (urlPath.contains("/walletsolidity")) {
          httpPort = Args.getInstance().getSolidityHttpPort();
        } else if (urlPath.contains("/walletpbft")) {
          httpPort = Args.getInstance().getPBFTHttpPort();
        } else {
          httpPort = Args.getInstance().getFullNodeHttpPort();
        }

        String url = String.format("http://%s:%d%s", ip, httpPort, urlPath);

        Args.getInstance().setDisabledApiList(disabledApiList);
        String response = sendGetRequest(url);
        Assert.assertEquals("{\"Error\":\"this API is unavailable due to config\"}",
            response);

        Args.getInstance().setDisabledApiList(emptyList);
        int statusCode = getRequestCode(url);
        Assert.assertEquals(HttpStatus.SC_OK, statusCode);
      }
    }
  }
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L127-151)
```java
  @Test
  public void testIsDisabled() throws Exception {
    List<String> list = new ArrayList<>();
    list.add("getnowblock");
    CommonParameter.getInstance().setDisabledApiList(list);
    Method privateMethod = httpApiAccessFilter.getClass()
            .getDeclaredMethod("isDisabled", String.class);
    privateMethod.setAccessible(true);

    String url = "/wallet/getnowblock";
    boolean f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/../b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);

    url = "/wallet/getblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);
  }
```

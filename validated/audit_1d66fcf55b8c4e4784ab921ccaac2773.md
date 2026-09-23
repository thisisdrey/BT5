### Title
Unbounded recursion in the compiler's own expression parser can stack-overflow `cmd/compile` on attacker-supplied Go source - ([File: src/cmd/compile/internal/syntax/parser.go])

### Summary
The BIRD CVE is a classic "recursive-descent parser has no depth limit → stack consumption → process crash" bug. In this repository, `go/parser` (the reusable, tool-facing Go parser) was explicitly hardened against exactly this class of bug with a `nestLev` counter and `incNestLev`/`decNestLev` checks that bail out once `maxNestLev` (1e5) is exceeded. [1](#0-0) [2](#0-1) [3](#0-2)  By contrast, the compiler's own front-end parser in `cmd/compile/internal/syntax` — the parser that `cmd/compile` actually uses to parse every `.go` file during `go build` — contains no analogous depth-tracking field or check anywhere in `parser.go`; its mutually recursive `expr`/`binaryExpr`/`unaryExpr` functions recurse directly on nested syntax with no bound. [4](#0-3) 

### Finding Description
Attacker input: a `.go` source file (e.g., inside a downloaded/vendored module or any source a developer compiles) containing a deeply nested expression, such as thousands of nested unary operators or parenthesized/binary sub-expressions.
Go entry point: `cmd/compile` invokes `syntax.Parse` → `(*parser).fileOrNil` → ... → `(*parser).expr` → `(*parser).binaryExpr` (which recurses into itself via `t.Y = p.binaryExpr(nil, tprec)`) and `(*parser).unaryExpr`. [5](#0-4) 
Failed check: unlike `go/parser`, this recursive descent has no `nestLev`/`maxNestLev`-style guard, so there is nothing that converts excessive nesting into a graceful parse error via `panic(bailout{})` before the Go runtime's own stack-growth machinery is exhausted.
Sink: sufficiently deep nesting causes the process to hit the runtime stack limit, which is a fatal, non-recoverable runtime error (`runtime: goroutine stack exceeds ... - fatal error: stack overflow`), crashing the entire `cmd/compile` process invoked by `go build`.

### Impact Explanation
This is a denial-of-service against the Go toolchain itself: an ordinary developer running `go build`/`go vet`/`gofmt`-adjacent tooling on an attacker-supplied or malicious third-party module/source file can crash the compiler process outright, rather than receiving a normal syntax error. This does not involve executing attacker code (consistent with the Go security policy that "go build must not run malicious source" — the compiler *parsing* untrusted source is exactly the boundary that must remain safe, distinct from *running* built code). A crash induced purely by the parser's own bounded-stack recursion during parsing (not compilation of huge/valid programs, but a pathological small-input trigger) would be assessable on Go's PUBLIC security track as a parser-panic/crash class issue.

### Likelihood Explanation
Victim workflow: any `go build`, `go vet`, or IDE/gopls-driven parse of a downloaded module, generated code, or fuzzed input file. Attacker capability required: only the ability to get a victim to compile/parse a crafted `.go` file — no privileged access, no malicious server, no TLS/crypto trust required, matching the "ordinary user data / published module source consumed by normal victim workflow" premise.

### Recommendation
Port the same defense already present in `go/parser` (a `nestLev` field on `parser`, `incNestLev`/`decNestLev` helpers, and a `maxNestLev` bound triggering a parse-error + `panic(bailout{})`) into `cmd/compile/internal/syntax`'s `binaryExpr`, `unaryExpr`, and other mutually recursive expression/type parsing entry points, so pathological nesting degrades to a bounded syntax error instead of runtime stack exhaustion.

### Proof of Concept
```go
// analog_test.go (place under src/cmd/compile/internal/syntax)
package syntax

import (
	"strings"
	"testing"
)

// Mirrors go/parser's TestParseDepthLimit, but cmd/compile/internal/syntax
// has no equivalent guard, so this either crashes the process with a fatal
// "stack overflow" runtime error, or (if it happens to survive) never
// produces the "exceeded max nesting depth" error that go/parser emits.
func TestSyntaxParserDeepNestingCrashesOrHangsWithoutDepthLimit(t *testing.T) {
	n := 2_000_000 // deep enough to exceed default goroutine max stack
	src := "package p\nvar x = " + strings.Repeat("-", n) + "1\n"

	errh := func(err error) { t.Log(err) }
	_, err := Parse(NewFileBase("x.go"), strings.NewReader(src), errh, nil, 0)
	// Expectation for a hardened parser (like go/parser): a bounded
	// "exceeded max nesting depth" error, no crash.
	if err == nil {
		t.Fatalf("expected a nesting-depth error, got none (potential stack overflow risk instead)")
	}
}
```
Note: I could not directly confirm from the index whether `cmd/compile/internal/syntax` has any other, less obvious recursion guard (e.g., a shared bailout/stack-size check elsewhere in the file that my search didn't surface) — a background Devin session with full file access should verify the complete `parser.go` and its call graph (`unaryExpr`, `primaryExpr`, `pexpr`, `type_`, `typeOrNil`, composite-literal parsing, etc.) before finalizing this as an unpatched issue, since the index only returned partial file contents for this file.

### Citations

**File:** src/go/parser/parser.go (L72-74)
```go
	// nestLev is used to track and limit the recursion depth
	// during parsing.
	nestLev int
```

**File:** src/go/parser/parser.go (L123-139)
```go
// maxNestLev is the deepest we're willing to recurse during parsing
const maxNestLev int = 1e5

func incNestLev(p *parser) *parser {
	p.nestLev++
	if p.nestLev > maxNestLev {
		p.error(p.pos, "exceeded max nesting depth")
		panic(bailout{})
	}
	return p
}

// decNestLev is used to track nesting depth during parsing to prevent stack exhaustion.
// It is used along with incNestLev in a similar fashion to how un and trace are used.
func decNestLev(p *parser) {
	p.nestLev--
}
```

**File:** src/go/parser/parser.go (L1866-1880)
```go
	// We track the nesting here rather than at the entry for the function,
	// since it can iteratively produce a nested output, and we want to
	// limit how deep a structure we generate.
	var n int
	defer func() { p.nestLev -= n }()
	for n = 1; ; n++ {
		incNestLev(p)
		op, oprec := p.tokPrec()
		if oprec < prec1 {
			return x
		}
		pos := p.expect(op)
		y := p.parseBinaryExpr(nil, oprec+1)
		x = &ast.BinaryExpr{X: x, OpPos: pos, Op: op, Y: y}
	}
```

**File:** src/cmd/compile/internal/syntax/parser.go (L864-890)
```go
func (p *parser) expr() Expr {
	if trace {
		defer p.trace("expr")()
	}

	return p.binaryExpr(nil, 0)
}

// Expression = UnaryExpr | Expression binary_op Expression .
func (p *parser) binaryExpr(x Expr, prec int) Expr {
	// don't trace binaryExpr - only leads to overly nested trace output

	if x == nil {
		x = p.unaryExpr()
	}
	for (p.tok == _Operator || p.tok == _Star) && p.prec > prec {
		t := new(Operation)
		t.pos = p.pos()
		t.Op = p.op
		tprec := p.prec
		p.next()
		t.X = x
		t.Y = p.binaryExpr(nil, tprec)
		x = t
	}
	return x
}
```

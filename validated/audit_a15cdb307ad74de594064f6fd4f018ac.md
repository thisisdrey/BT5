No vulnerability found for this question.

Every recursive descent parser in the Go standard library that handles untrusted nested structures analogous to the protobufjs JSON descriptor expansion issue already enforces an explicit depth limit to prevent stack overflow:

- `encoding/json/jsontext` enforces `maxNestingDepth = 10000` in both the state machine (`pushObject`/`pushArray`) and the streaming decoder (`consumeObject`/`consumeArray`), returning `errMaxDepth` once exceeded. [1](#0-0) [2](#0-1) [3](#0-2) 
- `encoding/xml` enforces `maxUnmarshalDepth` (with a lower `maxUnmarshalDepthWasm`) in `Decoder.unmarshal`, with regression tests explicitly named for prior CVEs (e.g. `TestCVE202228131`, `TestDecodeElementDepthBypass`). [4](#0-3) [5](#0-4) 
- `encoding/gob` enforces `maxIgnoreNestingDepth` when recursively building ignore-ops for nested/recursive types. [6](#0-5) 
- `text/template/parse` enforces `maxStackDepth` for nested parenthesized expressions, and `text/template/exec` enforces `maxExecDepth` for nested template invocations. [7](#0-6) [8](#0-7) 
- `go/parser` enforces `maxNestLev` via `incNestLev`/`decNestLev` to bound recursion depth during syntax parsing. [9](#0-8) 

Since the exact bug class described in the report (uncontrolled recursion / CWE-674 during nested descriptor/document expansion) is already mitigated with hard depth limits across every comparable Go stdlib parser, there is no reachable, unpatched analog to report.

### Citations

**File:** src/encoding/json/jsontext/state.go (L51-53)
```go
// Per RFC 8259, section 9, implementations may enforce a maximum depth.
// Such a limit is necessary to prevent stack overflows.
const maxNestingDepth = 10000
```

**File:** src/encoding/json/jsontext/state.go (L300-316)
```go
// pushObject appends a JSON begin object token as next in the sequence.
// If an error is returned, the state is not mutated.
func (m *stateMachine) pushObject() error {
	switch {
	case m.Last.NeedObjectName():
		return ErrNonStringName
	case !m.Last.isValidNamespace():
		return errInvalidNamespace
	case len(m.Stack) == maxNestingDepth:
		return errMaxDepth
	default:
		m.Last.Increment()
		m.Stack = append(m.Stack, m.Last)
		m.Last = stateTypeObject
		return nil
	}
}
```

**File:** src/encoding/json/jsontext/decode.go (L980-984)
```go
	if uint(pos) >= uint(len(d.buf)) || d.buf[pos] != '{' {
		panic("BUG: consumeObject must be called with a buffer that starts with '{'")
	} else if depth == maxNestingDepth+1 {
		return pos, errMaxDepth
	}
```

**File:** src/encoding/xml/read.go (L318-329)
```go
const (
	maxUnmarshalDepth     = 10000
	maxUnmarshalDepthWasm = 5000 // go.dev/issue/56498
)

var errUnmarshalDepth = errors.New("exceeded max depth")

// Unmarshal a single XML element into val.
func (d *Decoder) unmarshal(val reflect.Value, start *StartElement) error {
	if d.stkDepth > maxUnmarshalDepth || runtime.GOARCH == "wasm" && d.stkDepth > maxUnmarshalDepthWasm {
		return errUnmarshalDepth
	}
```

**File:** src/encoding/xml/read_test.go (L1102-1113)
```go
func TestCVE202228131(t *testing.T) {
	type nested struct {
		Parent *nested `xml:",any"`
	}
	var n nested
	err := Unmarshal(bytes.Repeat([]byte("<a>"), maxUnmarshalDepth+1), &n)
	if err == nil {
		t.Fatal("Unmarshal did not fail")
	} else if !errors.Is(err, errUnmarshalDepth) {
		t.Fatalf("Unmarshal unexpected error: got %q, want %q", err, errUnmarshalDepth)
	}
}
```

**File:** src/encoding/gob/decode.go (L920-929)
```go
var maxIgnoreNestingDepth = 10000

// decIgnoreOpFor returns the decoding op for a field that has no destination.
func (dec *Decoder) decIgnoreOpFor(wireId typeId, inProgress map[typeId]*decOp) *decOp {
	// Track how deep we've recursed trying to skip nested ignored fields.
	dec.ignoreDepth++
	defer func() { dec.ignoreDepth-- }()
	if dec.ignoreDepth > maxIgnoreNestingDepth {
		error_(errors.New("invalid nesting depth"))
	}
```

**File:** src/text/template/parse/parse.go (L49-58)
```go
// maxStackDepth is the maximum depth permitted for nested
// parenthesized expressions.
var maxStackDepth = 10000

// init reduces maxStackDepth for WebAssembly due to its smaller stack size.
func init() {
	if runtime.GOARCH == "wasm" {
		maxStackDepth = 1000
	}
}
```

**File:** src/text/template/exec.go (L18-28)
```go
// maxExecDepth specifies the maximum stack depth of templates within
// templates. This limit is only practically reached by accidentally
// recursive template invocations. This limit allows us to return
// an error instead of triggering a stack overflow.
var maxExecDepth = initMaxExecDepth()

func initMaxExecDepth() int {
	if runtime.GOARCH == "wasm" {
		return 1000
	}
	return 100000
```

**File:** src/go/parser/parser.go (L123-133)
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
```

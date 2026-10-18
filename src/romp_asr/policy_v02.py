"""Bounded ROMP-mu++/0.2 compilation and deterministic pure evaluation."""

from dataclasses import dataclass
import hashlib
import json


ACTIONS = ("ALLOW", "REJECT", "ESCALATE")
BOUNDS = {"max_rules": 64, "max_depth": 6, "max_nodes": 1024}


class CompileError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value, limit=1024):
    try:
        return type(value) is str and len(value) <= limit and len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _scalar(value, kind):
    if kind == "int":
        return type(value) is int and -(2**63) <= value < 2**63
    if kind == "bool":
        return type(value) is bool
    return kind == "string" and _text(value)


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise CompileError("invalid or unknown object keys")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CompileError("duplicate object key")
        result[key] = value
    return result


def _load(text, format):
    if not isinstance(text, (str, bytes)) or len(text) > 65536:
        raise CompileError("policy byte limit")
    try:
        text = text.decode("utf-8") if isinstance(text, bytes) else text
        if len(text.encode("utf-8")) > 65536:
            raise CompileError("policy byte limit")
        if format == "json":
            # Check nesting before invoking the recursive JSON decoder.
            depth, quoted, escaped = 0, False, False
            for char in text:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "[{":
                    depth += 1
                    if depth > 16:
                        raise CompileError("encoded structure too deep")
                elif char in "]}":
                    depth -= 1
            return json.loads(text, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(
                                  CompileError("non-finite constant")))
        if format != "yaml":
            raise CompileError("unsupported format")
        import yaml

        class RestrictedLoader(yaml.SafeLoader):
            pass

        def mapping(loader, node):
            return _pairs([(loader.construct_object(k), loader.construct_object(v))
                           for k, v in node.value])

        RestrictedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        depth = 0
        for event in yaml.parse(text, Loader=RestrictedLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise CompileError("YAML anchors and aliases are prohibited")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 16:
                    raise CompileError("encoded structure too deep")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        return yaml.load(text, Loader=RestrictedLoader)
    except CompileError:
        raise
    except Exception as exc:
        raise CompileError("invalid policy encoding") from exc


@dataclass(frozen=True)
class CompiledPolicy:
    canonical: str
    policy_hash: str


@dataclass(frozen=True)
class DecisionRecord:
    decision: str
    reason: str
    policy_hash: str
    context_hash: str
    hard_ids: tuple = ()
    soft_ids: tuple = ()
    scores: tuple = ()
    path: tuple = ()
    version: str = "romp-mu++/0.2"


def compile_policy(text, format="json"):
    try:
        return _compile_policy(text, format)
    except CompileError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise CompileError("invalid policy structure") from exc


def _compile_policy(text, format):
    doc = _load(text, format)
    _keys(doc, ("version", "types", "default", "rules", "order", "limits"))
    if doc["version"] != "romp-mu++/0.2" or doc["default"] not in ("REJECT", "ESCALATE"):
        raise CompileError("unsupported version or unsafe default")
    types = doc["types"]
    if type(types) is not dict or len(types) > 64 or any(
        not _text(k) or not k or v not in ("int", "bool", "string")
        for k, v in types.items()
    ):
        raise CompileError("invalid field types")
    limits = doc["limits"]
    _keys(limits, BOUNDS)
    if any(type(limits[k]) is not int or not 1 <= limits[k] <= BOUNDS[k] for k in BOUNDS):
        raise CompileError("invalid limits")
    rules = doc["rules"]
    if type(rules) is not list or len(rules) > limits["max_rules"]:
        raise CompileError("rule limit")
    nodes = 0

    def expression(expr, depth=1):
        nonlocal nodes
        nodes += 1
        if nodes > limits["max_nodes"] or depth > limits["max_depth"]:
            raise CompileError("expression limit")
        if type(expr) is not list or not expr or type(expr[0]) is not str:
            raise CompileError("invalid expression")
        op = expr[0]
        if op in ("AND", "OR"):
            if len(expr) < 3:
                raise CompileError("AND/OR require at least two operands")
            children = []
            for child in expr[1:]:
                child = expression(child, depth + 1)
                children.extend(child[1:] if child[0] == op else [child])
            unique = {canonical_json(child): child for child in children}
            children = [unique[k] for k in sorted(unique)]
            return children[0] if len(children) == 1 else [op, *children]
        if op == "NOT":
            if len(expr) != 2:
                raise CompileError("NOT arity")
            return [op, expression(expr[1], depth + 1)]
        if op not in (">", "<", "=", "IN") or len(expr) != 3:
            raise CompileError("invalid atom")
        _, field, value = expr
        if type(field) is not str or field not in types:
            raise CompileError("undeclared field")
        kind = types[field]
        if op in (">", "<") and kind != "int":
            raise CompileError("numeric comparison requires int")
        if op == "IN":
            if type(value) is not list or len(value) > 64 or any(
                not _scalar(v, kind) for v in value
            ):
                raise CompileError("invalid IN constant")
            values = {canonical_json(v): v for v in value}
            value = [values[k] for k in sorted(values)]
        elif not _scalar(value, kind):
            raise CompileError("constant type mismatch")
        return [op, field, value]

    ids = set()
    normalized = []
    for rule in rules:
        if type(rule) is not dict:
            raise CompileError("invalid rule")
        kind = rule.get("kind")
        fields = ("id", "kind", "if", "then")
        _keys(rule, fields if kind == "hard" else (*fields, "weight", "priority"))
        if not _text(rule["id"], 64) or not rule["id"] or rule["id"] in ids:
            raise CompileError("invalid or duplicate rule ID")
        ids.add(rule["id"])
        if kind == "hard":
            if rule["then"] != "REJECT":
                raise CompileError("hard rules must reject")
        elif kind == "soft":
            if (rule["then"] not in ACTIONS or type(rule["weight"]) is not int
                    or not 1 <= rule["weight"] <= 1000000
                    or type(rule["priority"]) is not int
                    or not -(2**31) <= rule["priority"] < 2**31):
                raise CompileError("invalid soft rule")
        else:
            raise CompileError("unknown rule kind")
        normalized.append({**rule, "if": expression(rule["if"])})
    _keys(doc["order"], ("precedence",))
    edges = doc["order"]["precedence"]
    if type(edges) is not list or len(edges) > 6:
        raise CompileError("invalid precedence")
    reach = set()
    for edge in edges:
        if type(edge) is not list or len(edge) != 2 or any(x not in ACTIONS for x in edge):
            raise CompileError("invalid precedence edge")
        reach.add(tuple(edge))
    for middle in ACTIONS:
        reach |= {(a, b) for a in ACTIONS for b in ACTIONS
                  if (a, middle) in reach and (middle, b) in reach}
    if any((a, a) in reach for a in ACTIONS):
        raise CompileError("precedence cycle")
    doc["rules"] = sorted(normalized, key=lambda r: r["id"])
    doc["order"]["precedence"] = [list(e) for e in sorted(reach)]
    encoded = canonical_json(doc)
    return CompiledPolicy(encoded, _hash(encoded))


def evaluate(policy, context):
    doc = json.loads(policy.canonical)
    try:
        if type(context) is not dict or set(context) != set(doc["types"]):
            raise ValueError()
        if any(not _scalar(context[k], kind) for k, kind in doc["types"].items()):
            raise ValueError()
        encoded = canonical_json(context)
        if len(encoded.encode("utf-8")) > 16384:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        return DecisionRecord("REJECT", "INVALID_CONTEXT", policy.policy_hash, "")

    def predicate(expr):
        op = expr[0]
        if op == "AND":
            return all(predicate(e) for e in expr[1:])
        if op == "OR":
            return any(predicate(e) for e in expr[1:])
        if op == "NOT":
            return not predicate(expr[1])
        left, right = context[expr[1]], expr[2]
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == "IN":
            return left in right
        return left == right

    base = (policy.policy_hash, _hash(encoded))
    hard = tuple(r["id"] for r in doc["rules"]
                 if r["kind"] == "hard" and predicate(r["if"]))
    if hard:
        return DecisionRecord("REJECT", "HARD_REJECT", *base, hard_ids=hard, path=("hard",))
    matched = [r for r in doc["rules"] if r["kind"] == "soft" and predicate(r["if"])]
    if not matched:
        return DecisionRecord(doc["default"], "DEFAULT", *base, path=("default",))
    scores, priorities = {}, {}
    for rule in matched:
        action = rule["then"]
        scores[action] = scores.get(action, 0) + rule["weight"]
        priorities[action] = max(priorities.get(action, -(2**31)), rule["priority"])
    candidates = {a for a in scores if scores[a] == max(scores.values())}
    path = ["score"]
    if len(candidates) > 1:
        priority = max(priorities[a] for a in candidates)
        candidates = {a for a in candidates if priorities[a] == priority}
        path.append("priority")
    if len(candidates) > 1:
        edges = {tuple(e) for e in doc["order"]["precedence"]}
        candidates = {a for a in candidates if not any(
            (b, a) in edges for b in candidates)}
        path.append("precedence")
    decision = next(iter(candidates)) if len(candidates) == 1 else "ESCALATE"
    if len(candidates) != 1:
        path.append("unresolved")
    return DecisionRecord(decision, "SOFT_DECISION", *base,
                          soft_ids=tuple(r["id"] for r in matched),
                          scores=tuple(sorted(scores.items())), path=tuple(path))

"""Configurable reference Agent; the host alone invokes registered tools."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from .policy_v02 import canonical_json
from .policy_v03 import compile_state_policy
from .state_effect_tools import StateEffectAdapter, StateEffectExecutor
from .tool_backends import LocalPaymentLedger, NoEffectError, PosixFileTools, ToolResult


class AgentError(Exception):
    """Safe operational error suitable for a CLI diagnostic."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Do not forward model authorization to a redirected endpoint.
        return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text(value):
    return type(value) is str and 0 < len(value) <= 1024


def _inside(path, root):
    return path == root or root in path.parents


def _charge_adapter(payments):
    """Facts describe the request; only the policy declares its effects."""
    def facts(subject, args):
        amount = args.get("amount_minor") if set(args) == {"amount_minor"} else None
        if type(amount) is not int or not 0 < amount < 2**63:
            raise NoEffectError("invalid amount")
        return {"amount_minor": amount, "target_protected": False}

    return StateEffectAdapter(facts, lambda identifier, args: payments.charge(
        identifier, args["amount_minor"]), "charge-v1")


def _delete_adapter(files, protected_roots):
    protected = frozenset(protected_roots)
    if not protected <= set(files.roots):
        raise ValueError("protected root must be mapped")

    def facts(subject, args):
        if (set(args) != {"root", "path"} or
                not all(type(v) is str and v for v in args.values()) or
                args["root"] not in files.roots):
            raise NoEffectError("invalid target")
        files.delete(args["root"], args["path"], validate_only=True)
        return {"amount_minor": 0, "target_protected": args["root"] in protected}

    binding = canonical_json({"roots": files.roots, "protected": sorted(protected)})
    return StateEffectAdapter(facts, lambda identifier, args: files.delete(
        args["root"], args["path"]), binding)


def load_config(filename):
    filename = Path(filename).resolve()
    try:
        raw = filename.read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        config = json.loads(raw, object_pairs_hook=_unique)
        required = {"policy", "state_dir", "subject", "model", "orders"}
        optional = {"roots", "protected_roots", "targets", "max_turns", "max_tokens"}
        if (type(config) is not dict or not required <= config.keys()
                or config.keys() - required - optional):
            raise ValueError()
        for key in ("policy", "state_dir", "subject"):
            if not _text(config[key]):
                raise ValueError()
        model = config["model"]
        if (type(model) is not dict or set(model) != {"endpoint", "name", "key_env"}
                or not all(_text(v) for v in model.values())):
            raise ValueError()
        url = urlsplit(model["endpoint"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path.endswith("/chat/completions")):
            raise ValueError()
        for key, default, upper in (("max_turns", 8, 32), ("max_tokens", 4096, 16384)):
            config.setdefault(key, default)
            if type(config[key]) is not int or not 1 <= config[key] <= upper:
                raise ValueError()
        orders = config["orders"]
        if type(orders) is not dict or len(orders) > 128:
            raise ValueError()
        ids = set()
        for alias, order in orders.items():
            if (not _text(alias) or type(order) is not dict
                    or set(order) != {"id", "amount_minor"} or not _text(order["id"])
                    or type(order["amount_minor"]) is not int
                    or not 0 < order["amount_minor"] < 2**63 or order["id"] in ids):
                raise ValueError()
            ids.add(order["id"])
        config.setdefault("roots", {})
        config.setdefault("protected_roots", [])
        config.setdefault("targets", {})
        roots, protected, targets = (config[k] for k in
                                      ("roots", "protected_roots", "targets"))
        if (type(roots) is not dict or len(roots) > 32
                or not all(_text(k) and _text(v) for k, v in roots.items())
                or type(protected) is not list or not all(_text(v) for v in protected)
                or len(set(protected)) != len(protected) or not set(protected) <= roots.keys()
                or type(targets) is not dict or len(targets) > 128
                or bool(roots) != bool(targets) or not (orders or targets)):
            raise ValueError()
        for alias, target in targets.items():
            if (not _text(alias) or type(target) is not dict
                    or set(target) != {"id", "root", "path"}
                    or not all(_text(v) for v in target.values())
                    or target["root"] not in roots or target["id"] in ids):
                raise ValueError()
            parts = target["path"].split("/")
            if "\\" in target["path"] or any(p in ("", ".", "..") for p in parts):
                raise ValueError()
            ids.add(target["id"])
        # Preserve lexical paths so the file backend can reject symlink roots.
        def absolute(value):
            return Path(os.path.abspath(filename.parent / value))
        config["policy"] = absolute(config["policy"])
        config["state_dir"] = absolute(config["state_dir"])
        config["roots"] = {k: absolute(v) for k, v in roots.items()}
        for root in config["roots"].values():
            if (_inside(config["state_dir"].resolve(), root.resolve())
                    or _inside(filename, root.resolve())
                    or _inside(config["policy"].resolve(), root.resolve())):
                raise ValueError()
        return config
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise AgentError("invalid host configuration") from None


class ModelClient:
    def __init__(self, config):
        self.model = config["model"]
        self.max_tokens = config["max_tokens"]
        self.key = os.environ.get(self.model["key_env"])
        if not self.key:
            raise AgentError("model key environment variable is unset")

    def complete(self, messages, tools):
        body = {"model": self.model["name"], "messages": messages, "tools": tools,
                "tool_choice": "auto", "temperature": 0, "max_tokens": self.max_tokens}
        request = urllib.request.Request(
            self.model["endpoint"], json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=180) as response:
                raw = response.read(2_097_153)
            if len(raw) > 2_097_152:
                raise AgentError("model response exceeds byte limit")
            result = json.loads(raw)
            return json.loads(json.dumps(result).replace(self.key, "[REDACTED]"))
        except urllib.error.HTTPError as exc:
            raise AgentError("model HTTP status " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AgentError("model transport failure") from None
        except (ValueError, RecursionError):
            raise AgentError("invalid model JSON") from None


class AgentHost:
    def __init__(self, config, *, inspect_only=False):
        self.config, self.files = config, None
        state = config["state_dir"]
        requests, payments = state / "requests.db", state / "payments.db"
        if (any(p.is_symlink() for p in (state, *state.parents))
                or any(p.is_symlink() for p in (requests, payments, state / "agent-trace.jsonl"))):
            raise AgentError("persistent scope contains symbolic links")
        if requests.exists() != payments.exists() or (inspect_only and not requests.exists()):
            raise AgentError("incomplete or absent persistent scope")
        # Configure/validate backends before creating persistent scope records.
        if config["roots"]:
            self.files = PosixFileTools(config["roots"])
        try:
            policy_bytes = config["policy"].read_bytes()
            if len(policy_bytes) > 65536:
                raise AgentError("policy exceeds byte limit")
            policy = compile_state_policy(policy_bytes, "yaml" if
                                          config["policy"].suffix.lower() in (".yaml", ".yml") else "json")
            catalog = canonical_json({
                "subject": config["subject"], "orders": config["orders"],
                "roots": {k: str(v) for k, v in config["roots"].items()},
                "protected_roots": sorted(config["protected_roots"]),
                "targets": config["targets"],
            })
            catalog_hash = hashlib.sha256(catalog.encode()).hexdigest()
            state.mkdir(parents=True, exist_ok=True)
            self.payments = LocalPaymentLedger(payments)
            adapters = {}
            if config["orders"]:
                adapters["Charge"] = _charge_adapter(self.payments)
            if self.files:
                adapters["Delete"] = _delete_adapter(self.files, config["protected_roots"])
            adapters = {k: StateEffectAdapter(v.facts, v.invoke, v.binding + ":" + catalog_hash)
                        for k, v in adapters.items()}
            self.executor = StateEffectExecutor(policy, requests, adapters)
        except Exception:
            self.close()
            raise

    @property
    def tools(self):
        tools = []
        for name, field, catalog in (
                ("charge", "order", self.config["orders"]),
                ("delete", "target", self.config["targets"])):
            if catalog:
                tools.append({"type": "function", "function": {
                    "name": name, "description": "Request a policy-controlled " + name + ".",
                    "parameters": {"type": "object", "properties": {
                        field: {"type": "string", "enum": list(catalog)}},
                        "required": [field], "additionalProperties": False}}})
        return tools

    def submit(self, name, args):
        if type(args) is not dict:
            return ToolResult("REJECT", "invalid tool arguments")
        if name == "charge" and set(args) == {"order"} and _text(args["order"]):
            order = self.config["orders"].get(args["order"])
            if order:
                return self.executor.submit(order["id"], self.config["subject"], "Charge",
                                            {"amount_minor": order["amount_minor"]})
        if name == "delete" and set(args) == {"target"} and _text(args["target"]):
            target = self.config["targets"].get(args["target"])
            if target:
                return self.executor.submit(target["id"], self.config["subject"], "Delete",
                                            {"root": target["root"], "path": target["path"]})
        return ToolResult("REJECT", "unknown tool or resource")

    def status(self):
        snapshot = self.executor.snapshot()
        return {"payment_total_minor": self.payments.total,
                "held": snapshot["held"],
                "requests": snapshot["requests"]}

    def close(self):
        if self.files:
            self.files.close()


class ReferenceAgent:
    def __init__(self, host, client):
        self.host, self.client = host, client

    def run(self, task):
        if type(task) is not str or not task.strip() or len(task.encode("utf-8")) > 16384:
            raise AgentError("invalid task")
        run_id = uuid.uuid4().hex
        messages = [{"role": "system", "content": (
            "Propose operations using only the provided tools. The trusted host enforces "
            "its policy and owns all resources. Respect REJECT and ESCALATE. UNKNOWN "
            "does not confirm success or absence of effects. Retrying a resource uses "
            "the same host business ID. Report tool outcomes honestly; do not claim "
            "denied or uncertain operations succeeded.")},
                    {"role": "user", "content": task}]
        trace = self.host.config["state_dir"] / "agent-trace.jsonl"
        def record(kind, **values):
            # Trace is separate from authoritative transactional accounting.
            with trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"run_id": run_id, "kind": kind, **values}) + "\n")
        record("task", task=task)
        termination, answer = "turn_limit", None
        try:
            for turn in range(self.host.config["max_turns"]):
                response = self.client.complete(messages, self.host.tools)
                message, finish = self._message(response)
                record("model", turn=turn, model=response.get("model"),
                       response_id=response.get("id"), usage=response.get("usage"),
                       finish_reason=finish, message=message)
                calls = message.get("tool_calls") or []
                if len(calls) > 8:
                    termination = "tool_call_limit"
                    break
                messages.append(message)
                if not calls:
                    if finish != "stop":
                        raise AgentError("model output incomplete")
                    termination, answer = "completed", message.get("content")
                    break
                for call in calls:
                    try:
                        args = json.loads(call["function"]["arguments"], object_pairs_hook=_unique)
                    except (ValueError, RecursionError):
                        args = None
                    result = self.host.submit(call["function"]["name"], args)
                    record("tool", turn=turn, call=call, result=asdict(result))
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(asdict(result))})
        except AgentError as exc:
            record("termination", termination="error", reason=str(exc))
            raise
        except Exception:
            record("termination", termination="error", reason="host execution failure")
            raise AgentError("host execution failure") from None
        record("termination", termination=termination, answer=answer)
        return {"run_id": run_id, "termination": termination, "answer": answer,
                "state": self.host.status()}

    @staticmethod
    def _message(response):
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if message["role"] != "assistant" or not isinstance(message.get("content"), (str, type(None))):
                raise ValueError()
            calls = message.get("tool_calls") or []
            if type(calls) is not list:
                raise ValueError()
            ids = set()
            for call in calls:
                if (call["type"] != "function" or not _text(call["id"]) or call["id"] in ids
                        or not _text(call["function"]["name"])
                        or type(call["function"]["arguments"]) is not str
                        or len(call["function"]["arguments"].encode()) > 16384):
                    raise ValueError()
                ids.add(call["id"])
            if calls and choice.get("finish_reason") != "tool_calls":
                raise ValueError()
            return {k: message[k] for k in ("role", "content", "tool_calls") if k in message}, choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, ValueError):
            raise AgentError("invalid native tool-call response") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROMP policy-controlled reference Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "run":
            command.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    host = None
    try:
        config = load_config(args.config)
        client = ModelClient(config) if args.command == "run" else None
        host = AgentHost(config, inspect_only=args.command == "status")
        result = host.status() if args.command == "status" else ReferenceAgent(host, client).run(args.task)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if args.command == "status" or result["termination"] == "completed" else 1
    except Exception as exc:
        print(str(exc) if isinstance(exc, AgentError) else
              "host initialization or storage failure", file=sys.stderr)
        return 2
    finally:
        if host:
            host.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""AI-interpretation layer. It only writes prose about an already-computed report.

 * DeterministicInterpreter - template text, always available, clearly labelled.
 * AnthropicInterpreter     - optional; sends the structured report summary to the Anthropic Messages API and
                              stores the returned text. The model never writes Cypher, never changes a score and
                              falls back to the deterministic text on any error.
"""
from __future__ import annotations

import json
from typing import Protocol

import httpx

NOT_PROOF = "This is a risk assessment to prioritise human review. It is not proof of fraud."


class Interpreter(Protocol):
    def interpret(self, core: dict) -> dict: ...


def _top(core: dict, kind: str, n: int = 2) -> list[str]:
    return [e["text"] for e in core["evidence"][kind][:n]]


class DeterministicInterpreter:
    mode = "deterministic_template"

    def interpret(self, core: dict) -> dict:
        r = core["risk"]
        active = [c for c in r["components"] if c["points"] > 0]
        active.sort(key=lambda c: -abs(c["points"]))
        drivers = ", ".join(f"{c['name']} ({c['points']:+.1f})" for c in active[:4]) or "no scoring component fired"
        parts = [f"Risk assessment: {r['score']:.0f}/100 ({r['band']}). Main contributors: {drivers}."]
        sup, con = _top(core, "supporting"), _top(core, "contradicting")
        if sup:
            parts.append("Supporting evidence: " + " ".join(sup))
        if con:
            parts.append("Contradicting evidence: " + " ".join(con))
        if core["evidence"]["missing"]:
            parts.append(f"{len(core['evidence']['missing'])} evidence gap(s) limit confidence.")
        parts.append(NOT_PROOF)
        return {"mode": self.mode, "model": None, "label": "Deterministic template (no LLM used)", "text": " ".join(parts)}


class AnthropicInterpreter:
    mode = "llm"

    def __init__(self, api_key: str, model: str, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self.api_key, self.model = api_key, model
        self._client = httpx.Client(transport=transport, timeout=timeout)
        self._fallback = DeterministicInterpreter()

    def interpret(self, core: dict) -> dict:
        summary = {"risk": {k: core["risk"][k] for k in ("score", "band")},
                   "components": [{"name": c["name"], "points": c["points"]} for c in core["risk"]["components"] if c["points"]],
                   "supporting": _top(core, "supporting", 4), "contradicting": _top(core, "contradicting", 4),
                   "missing": _top(core, "missing", 3), "benign_explanations": core["benign_explanations"][:4]}
        prompt = ("You are explaining a fraud-risk ASSESSMENT to an analyst. Use ONLY the JSON facts below; add no new facts, "
                  "do not claim fraud is proven, mention one benign explanation and the main gap. 4 sentences max.\n" + json.dumps(summary))
        try:
            resp = self._client.post("https://api.anthropic.com/v1/messages", headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": self.model, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
            resp.raise_for_status()
            text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text").strip()
            if not text or len(text) > 3000:
                raise ValueError("empty or oversized LLM output")
            return {"mode": self.mode, "model": self.model, "label": f"LLM-written text ({self.model}); score computed by deterministic rules",
                    "text": text + " " + NOT_PROOF}
        except Exception as exc:  # never break an investigation because the optional LLM failed
            out = self._fallback.interpret(core)
            out.update(mode="deterministic_fallback", label=f"LLM unavailable ({type(exc).__name__}); deterministic template used")
            return out


def make_interpreter(settings) -> Interpreter:
    if settings.llm_mode == "anthropic" and settings.anthropic_api_key:
        return AnthropicInterpreter(settings.anthropic_api_key, settings.llm_model)
    return DeterministicInterpreter()

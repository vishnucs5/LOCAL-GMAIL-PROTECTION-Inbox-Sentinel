from __future__ import annotations

import json
from typing import Protocol

import httpx

from app.config import Settings
from app.services.types import ClassificationDecision, MessageEnvelope


SYSTEM_PROMPT = """
You classify incoming email as spam or not_spam.
Return valid JSON with this schema only:
{"verdict":"spam"|"not_spam","confidence":0.0-1.0,"reasons":["short reason","short reason"]}
Base the decision on sender reputation signals, urgency, scams, phishing traits, suspicious formatting, and irrelevant promotional content.
Do not include markdown or extra keys.
""".strip()

LOCAL_FALLBACK_MODEL = "local-heuristic-fallback"
FALLBACK_AUTO_LABEL_CAP = 0.84

SUSPICIOUS_RULES: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("urgent", "immediately", "act now", "final warning", "suspend", "suspended"), 2, "Urgent pressure language is present."),
    (("verify", "confirm", "login", "password", "credential", "otp", "code"), 2, "The message asks for account or credential action."),
    (("bank", "wire", "payment", "invoice", "refund", "crypto", "bitcoin", "gift card"), 2, "The content focuses on money movement or payment recovery."),
    (("claim", "winner", "prize", "lottery", "reward"), 2, "Prize or reward language is present."),
    (("click here", "http://", "https://", "bit.ly", "tinyurl", ".zip", ".html"), 1, "It includes a link or attachment pattern often used in scams."),
)


class ClassifierError(RuntimeError):
    pass


class SpamClassifier(Protocol):
    def classify(self, message: MessageEnvelope, max_body_chars: int) -> ClassificationDecision:
        raise NotImplementedError


def should_apply_spam_label(decision: ClassificationDecision, threshold: float) -> bool:
    return decision.verdict == "spam" and decision.confidence >= threshold


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


def _normalize_verdict(value: str) -> str:
    verdict = (value or "").strip().lower()
    if verdict not in {"spam", "not_spam"}:
        raise ClassifierError(f"Unexpected classifier verdict: {value!r}")
    return verdict


def _normalize_reasons(value: object) -> list[str]:
    if not isinstance(value, list):
        return ["Classifier returned no reasons."]

    reasons = [str(item).strip() for item in value if str(item).strip()]
    if not reasons:
        return ["Classifier returned no reasons."]
    return reasons[:3]


def _combined_message_text(message: MessageEnvelope, max_body_chars: int) -> str:
    return " ".join(
        [
            message.sender or "",
            message.subject or "",
            message.snippet or "",
            (message.normalized_text or "")[:max_body_chars],
        ]
    ).lower()


def _fallback_reason_for_exception(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "OpenAI rate-limited this scan, so a conservative local review was used."
    return "OpenAI was temporarily unavailable, so a conservative local review was used."


def _local_heuristic_classification(
    message: MessageEnvelope,
    max_body_chars: int,
    *,
    fallback_reason: str,
) -> ClassificationDecision:
    combined = _combined_message_text(message, max_body_chars)
    score = 0
    reasons: list[str] = [fallback_reason]

    for keywords, weight, reason in SUSPICIOUS_RULES:
        if any(keyword in combined for keyword in keywords):
            score += weight
            if reason not in reasons:
                reasons.append(reason)

    verdict = "spam" if score >= 3 else "not_spam"
    if verdict == "spam":
        confidence = 0.84 if score >= 6 else 0.72
    else:
        confidence = 0.28 if score == 0 else 0.4
        if len(reasons) == 1:
            reasons.append("No strong spam or phishing signals were detected.")

    return ClassificationDecision(
        verdict=verdict,
        confidence=min(confidence, FALLBACK_AUTO_LABEL_CAP),
        reasons=reasons[:3],
        provider_model=LOCAL_FALLBACK_MODEL,
    )


def _should_use_local_fallback(exc: Exception) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False


class OpenAICompatibleClassifier:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.ai_timeout_seconds)

    def classify(self, message: MessageEnvelope, max_body_chars: int) -> ClassificationDecision:
        if not self._settings.ai_api_key:
            raise ClassifierError("AI_API_KEY is not configured.")

        request_body = {
            "model": self._settings.ai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "sender": message.sender,
                            "subject": message.subject,
                            "snippet": message.snippet,
                            "body": message.normalized_text[:max_body_chars],
                        }
                    ),
                },
            ],
        }

        try:
            response = self._client.post(
                f"{self._settings.ai_api_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._settings.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()

            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(_strip_code_fences(content))
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if _should_use_local_fallback(exc):
                return _local_heuristic_classification(
                    message,
                    max_body_chars,
                    fallback_reason=_fallback_reason_for_exception(exc),
                )
            raise ClassifierError(f"Classifier request failed: {exc}") from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ClassifierError(f"Classifier request failed: {exc}") from exc

        return ClassificationDecision(
            verdict=_normalize_verdict(parsed.get("verdict", "")),
            confidence=confidence,
            reasons=_normalize_reasons(parsed.get("reasons")),
            provider_request_id=payload.get("id"),
            provider_model=payload.get("model"),
        )

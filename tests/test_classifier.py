from __future__ import annotations

import httpx

from app.services.classifier import LOCAL_FALLBACK_MODEL, OpenAICompatibleClassifier, should_apply_spam_label
from app.services.types import ClassificationDecision
from tests.conftest import make_envelope, make_settings


def test_label_applied_for_high_confidence_spam() -> None:
    decision = ClassificationDecision(verdict="spam", confidence=0.93, reasons=["Phishing phrases"])
    assert should_apply_spam_label(decision, threshold=0.85) is True


def test_label_not_applied_for_low_confidence_spam() -> None:
    decision = ClassificationDecision(verdict="spam", confidence=0.42, reasons=["Looks suspicious"])
    assert should_apply_spam_label(decision, threshold=0.85) is False


def test_label_not_applied_for_non_spam() -> None:
    decision = ClassificationDecision(verdict="not_spam", confidence=0.99, reasons=["Known sender"])
    assert should_apply_spam_label(decision, threshold=0.85) is False


def test_classifier_falls_back_to_local_review_on_openai_rate_limit(tmp_path) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, json={"error": {"message": "rate limited"}})

    classifier = OpenAICompatibleClassifier(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    message = make_envelope(
        "msg-429",
        subject="URGENT: verify your bank password",
        snippet="Click here immediately to protect your account",
        normalized_text="Your account will be suspended unless you log in at https://bit.ly/example right now.",
    )

    decision = classifier.classify(message, max_body_chars=8192)

    assert decision.provider_model == LOCAL_FALLBACK_MODEL
    assert decision.verdict == "spam"
    assert decision.confidence < 0.85
    assert any("local review" in reason.lower() or "rate-limit" in reason.lower() for reason in decision.reasons)


def test_classifier_fallback_stays_conservative_for_benign_mail(tmp_path) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, json={"error": {"message": "rate limited"}})

    classifier = OpenAICompatibleClassifier(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    message = make_envelope(
        "msg-safe",
        subject="Weekly engineering update",
        sender="teammate@example.com",
        snippet="Agenda for tomorrow's planning session",
        normalized_text="Project notes, roadmap updates, and lunch plans for the team.",
    )

    decision = classifier.classify(message, max_body_chars=8192)

    assert decision.provider_model == LOCAL_FALLBACK_MODEL
    assert decision.verdict == "not_spam"
    assert decision.confidence < 0.5
    assert should_apply_spam_label(decision, threshold=0.85) is False
